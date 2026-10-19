"""Exercise the frozen engine, multiprocessing, SSE, mono+dual and navigation restoration."""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import fitz
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from mock_provider import ProviderHandler, make_provider


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", type=Path, default=ROOT / "build/engine-dist/StudioEngine.app/Contents/MacOS/studio-engine")
    parser.add_argument("--seed-cache", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="packaged-smoke-", dir=ROOT / ".runtime") as temp:
        support = Path(temp)
        if args.seed_cache:
            for folder in ("fonts", "models", "cmap", "tiktoken"):
                shutil.copytree(ROOT / "backend/data/engine-cache" / folder, support / "engine-cache" / folder)
        provider = make_provider()
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        provider_url = f"http://127.0.0.1:{provider.server_port}/v1"
        state = support / "service.json"
        env = dict(os.environ, PDF_STUDIO_SUPPORT_DIR=str(support), PDF_STUDIO_STATE_FILE=str(state),
            PDF_STUDIO_SESSION_TOKEN="packaged-smoke-session", PDF_STUDIO_WEB_DIR=str(ROOT / "build/desktop-web"),
            PDF_STUDIO_PARENT_PID=str(os.getpid()), PATH="/usr/bin:/bin:/usr/sbin:/sbin")
        with (ROOT / ".runtime/packaged-smoke.log").open("w") as log:
            process = subprocess.Popen([str(args.engine)], env=env, stdout=log, stderr=log)
            try:
                for _ in range(240):
                    if process.poll() is not None:
                        raise RuntimeError("Frozen engine exited; see .runtime/packaged-smoke.log")
                    if state.exists():
                        break
                    time.sleep(0.25)
                else:
                    raise RuntimeError("Frozen engine did not write readiness state")
                base = f"http://127.0.0.1:{json.loads(state.read_text())['port']}"
                with httpx.Client(base_url=base, timeout=300, trust_env=False) as client:
                    assert client.get("/api/health").status_code == 403
                    client.headers["Authorization"] = "Bearer packaged-smoke-session"
                    for _ in range(80):
                        try:
                            response = client.get("/api/health")
                            if response.status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        time.sleep(0.25)
                    assert response.json()["ok"], response.text
                    assert "纸间" in client.get("/").text
                    assert client.get("/api/health", headers={"Origin": "https://foreign.test"}).status_code == 403
                    profile = {"base_url": provider_url, "model": "group-test-model", "api_key": "local-test-key", "qps": 2}
                    assert client.post("/api/desktop/profile", json=profile).status_code == 200
                    models = client.post("/api/provider/models", json={"base_url": provider_url}).json()
                    assert models["models"] == ["another-test-model", "group-test-model"], models
                    if not args.seed_cache:
                        print("Frozen startup, session isolation and provider discovery: PASS", flush=True)
                        return
                    source = support / "fixture.pdf"
                    document = fitz.open()
                    page = document.new_page()
                    page.insert_text((60, 70), "Research on language learning", fontsize=19)
                    paragraphs = ("Language learning depends on experience. Researchers compare different conditions and report their results. "
                        "This document examines the relationship between language exposure and learning. "
                        "The research includes several groups and considers the role of prior knowledge.\n\n") * 4
                    page.insert_textbox(fitz.Rect(60, 110, 530, 690), paragraphs, fontsize=12)
                    page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(60, 700, 250, 725), "uri": "https://example.test/research"})
                    document.set_metadata({"title": "Packaged integration fixture", "author": "Local test"})
                    document.set_toc([[1, "Research", 1]])
                    document.save(source)
                    document.close()
                    with source.open("rb") as file:
                        created = client.post("/api/jobs", files={"file": ("fixture.pdf", file, "application/pdf")},
                            data={"api_base_url": provider_url, "model": "group-test-model", "qps": "2", "generate_dual": "true"})
                    assert created.status_code == 202, created.text
                    job_id = created.json()["id"]
                    with client.stream("GET", f"/api/jobs/{job_id}/events") as events:
                        terminal = None
                        count = 0
                        for line in events.iter_lines():
                            if not line.startswith("data: "):
                                continue
                            event = json.loads(line[6:])
                            count += 1
                            if event["type"] in {"completed", "error", "cancelled"}:
                                terminal = event
                        assert terminal and terminal["type"] == "completed", terminal
                    results = {}
                    for kind in ("download", "download-dual"):
                        download = client.get(f"/api/jobs/{job_id}/{kind}")
                        assert download.status_code == 200, download.text
                        with fitz.open(stream=download.content, filetype="pdf") as output:
                            text = "".join(page.get_text() for page in output)
                            assert output.page_count == 1
                            assert "研究" in text, text[:800]
                            assert "Structure Rules" not in text
                            assert output.metadata["title"] == "Packaged integration fixture"
                            assert len(output.get_toc()) == 1
                            links = sum(len(page.get_links()) for page in output)
                            assert links == (2 if kind == "download-dual" else 1), links
                            results[kind] = {"bytes": len(download.content), "pages": output.page_count, "links": links}
                    report = {"checks": "frozen-multiprocessing-sse-mono-dual-navigation", "events": count,
                        "provider_requests": len(ProviderHandler.requests_seen), "outputs": results, "provider": "local deterministic mock"}
                    (ROOT / ".runtime/packaged-smoke-result.json").write_text(json.dumps(report, indent=2))
                    print(json.dumps(report, indent=2), flush=True)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                provider.shutdown()


if __name__ == "__main__":
    main()
