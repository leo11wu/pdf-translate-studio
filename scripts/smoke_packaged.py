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
                    for route, expected in (("source.txt", "Language learning"), ("translated.txt", "研究")):
                        text = client.get(f"/api/jobs/{job_id}/{route}")
                        assert text.status_code == 200, text.text
                        assert expected in text.text, text.text[:800]
                        assert "\x00" not in text.text and "\x01" not in text.text
                        assert "第 1 页" in text.text
                        results[route] = {"bytes": len(text.content), "utf8": True}
                    calls_before = len(ProviderHandler.requests_seen)
                    with source.open("rb") as file:
                        reused = client.post("/api/jobs", files={"file": ("fixture.pdf", file, "application/pdf")},
                            data={"api_base_url": provider_url, "model": "group-test-model", "qps": "2", "generate_dual": "true"})
                    assert reused.status_code == 202, reused.text
                    reused_id = reused.json()["id"]
                    with client.stream("GET", f"/api/jobs/{reused_id}/events") as events:
                        for line in events.iter_lines():
                            if line.startswith("data: "):
                                terminal = json.loads(line[6:])
                    assert terminal["type"] == "completed" and terminal["cache_hit"], terminal
                    assert len(ProviderHandler.requests_seen) == calls_before
                    assert client.get(f"/api/jobs/{reused_id}/download").content == client.get(f"/api/jobs/{job_id}/download").content
                    assert client.get("/api/history").json()["total"] == 2
                    report = {"checks": "frozen-multiprocessing-sse-mono-dual-navigation", "events": count,
                        "provider_requests": len(ProviderHandler.requests_seen), "outputs": results,
                        "verified_document_reuse_without_api_calls": True, "txt_exports": True,
                        "provider": "local deterministic mock"}
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=15)
                    state.unlink(missing_ok=True)
                    process = subprocess.Popen([str(args.engine)], env=env, stdout=log, stderr=log)
                    for _ in range(240):
                        if process.poll() is not None:
                            raise RuntimeError("Frozen restart failed")
                        if state.exists():
                            break
                        time.sleep(0.25)
                    restarted_base = f"http://127.0.0.1:{json.loads(state.read_text())['port']}"
                    with httpx.Client(base_url=restarted_base, timeout=30, trust_env=False,
                                      headers={"Authorization": "Bearer packaged-smoke-session"}) as restarted:
                        for _ in range(80):
                            try:
                                history = restarted.get("/api/history")
                                if history.status_code == 200:
                                    break
                            except httpx.ConnectError:
                                pass
                            time.sleep(0.25)
                        assert history.json()["total"] == 2, history.text
                        assert restarted.get(f"/api/jobs/{job_id}/source.txt").status_code == 200
                        assert restarted.get(f"/api/jobs/{job_id}/download").status_code == 200
                        report["restart_restores_history_and_downloads"] = True
                    for path in (support / "jobs").rglob("*"):
                        if path.is_file():
                            assert b"local-test-key" not in path.read_bytes(), f"Credential found in {path.name}"
                    report["job_files_do_not_contain_api_key"] = True
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
