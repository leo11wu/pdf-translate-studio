"""Frozen service entry point; freeze_support must precede heavy engine imports."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()

import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path


def main():
    os.umask(0o077)
    # Foundation.Process already creates a process group on macOS. setsid()
    # rejects a group leader, whereas subprocess.Popen needs us to create it.
    if os.getpgrp() != os.getpid():
        os.setsid()
    os.environ["PDF_STUDIO_DESKTOP"] = "1"
    support = Path(os.environ["PDF_STUDIO_SUPPORT_DIR"])
    support.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ["PDF_TRANSLATION_JOB_DIR"] = str(support / "jobs")
    os.environ["PDF_TRANSLATION_CACHE_DIR"] = str(support / "engine-cache")
    os.environ["XDG_CACHE_HOME"] = str(support / "cache")
    os.environ["XDG_CONFIG_HOME"] = str(support / "config")
    os.environ["PDF_TRANSLATION_MAX_ACTIVE_JOBS"] = "1"
    for name in list(os.environ):
        if name.startswith("PDF_TRANSLATION_API_") or name in {"API_CONFIG_PATH", "OPENAI_API_KEY", "OPENAI_BASE_URL"}:
            os.environ.pop(name)
    os.chdir(support)
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

    # Start watching immediately, even if an import or a network download stalls.
    parent = int(os.environ.get("PDF_STUDIO_PARENT_PID", "0"))
    def watch_parent():
        while parent:
            time.sleep(2)
            try:
                os.kill(parent, 0)
            except ProcessLookupError:
                os.killpg(os.getpgrp(), signal.SIGTERM)
                return
    threading.Thread(target=watch_parent, daemon=True).start()

    import app as worker
    import uvicorn
    from desktop_support import install_desktop_routes
    if worker.ENGINE_IMPORT_ERROR:
        raise RuntimeError(worker.ENGINE_IMPORT_ERROR)
    install_desktop_routes(worker, Path(os.environ["PDF_STUDIO_WEB_DIR"]), os.environ["PDF_STUDIO_SESSION_TOKEN"])
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    state = Path(os.environ["PDF_STUDIO_STATE_FILE"])
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = state.with_suffix(".tmp")
    temporary.write_text(json.dumps({"port": port}), encoding="utf-8")
    temporary.replace(state)
    config = uvicorn.Config(worker.app, host="127.0.0.1", port=port, log_level="warning",
                            access_log=False, loop="asyncio", http="h11", timeout_graceful_shutdown=8)
    try:
        uvicorn.Server(config).run(sockets=[listener])
    finally:
        state.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
