"""Desktop-only routes. Credentials are session memory supplied by AppKit."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


class SessionGuard:
    """Protect every resource, including SSE/downloads, without exposing a token to JS."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        expected_host = f"127.0.0.1:{scope['server'][1]}"
        host = headers.get(b"host", b"").decode()
        origin = headers.get(b"origin", b"").decode()
        bearer = headers.get(b"authorization", b"").decode()
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get(b"cookie", b"").decode())
        except Exception:
            pass
        supplied = cookie.get("studio_session")
        authorized = hmac.compare_digest(bearer, f"Bearer {self.token}") or bool(
            supplied and hmac.compare_digest(supplied.value, self.token)
        )
        native_only = scope["path"] == "/api/desktop/profile"
        if (host != expected_host or (origin and origin != f"http://{expected_host}")
                or not authorized or (native_only and bearer != f"Bearer {self.token}")):
            response = JSONResponse({"detail": "此连接不属于当前 App 会话。"}, status_code=403)
            return await response(scope, receive, send)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"),
                ])
            await send(message)
        await self.app(scope, receive, secure_send)


def provider_url(value: str, normalize) -> str:
    normalized = normalize(value)
    parsed = urlparse(normalized)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise HTTPException(422, "请填写完整的 http(s) API 基础地址，不要附带密钥、查询参数或片段。")
    try:
        parsed.port
    except ValueError as exc:
        raise HTTPException(422, "API 地址的端口无效。") from exc
    return normalized


from request_runtime import normalize_options


async def fetch_models(base_url: str, key: str, catalog: dict | None = None) -> list[str]:
    """Use the provider's standard /models endpoint; never forward keys on redirects."""
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            async with client.stream("GET", f"{base_url}/models", headers={
                "Authorization": f"Bearer {key}", "Accept": "application/json",
            }) as response:
                if response.status_code in {401, 403}:
                    raise HTTPException(422, "供应商拒绝访问，请检查 API 密钥及其权限。")
                if response.status_code in {404, 405, 501}:
                    raise HTTPException(422, "供应商没有提供模型列表，请手动填写模型名称。")
                if 300 <= response.status_code < 400:
                    raise HTTPException(422, "API 地址发生重定向。请改填供应商的最终 API 地址。")
                if response.status_code != 200:
                    raise HTTPException(422, f"模型列表请求失败（HTTP {response.status_code}），可稍后重试或手动填写。")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 2 * 1024 * 1024:
                        raise HTTPException(422, "供应商返回的模型列表过大，请手动填写。")
        payload = json.loads(content)
    except httpx.TimeoutException as exc:
        raise HTTPException(422, "获取模型列表超时，请检查网络或手动填写。") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(422, "无法连接此 API 地址，请检查地址、网络和证书。") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(422, "供应商返回的不是有效 JSON，可手动填写模型名称。") from exc
    entries = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise HTTPException(422, "无法识别供应商的模型列表，可手动填写模型名称。")
    names = set()
    for entry in entries[:10000]:
        name = entry.get("id", entry.get("name")) if isinstance(entry, dict) else entry
        if isinstance(name, str) and name.strip() and len(name) <= 512:
            names.add(name.strip())
            if catalog is not None and isinstance(entry, dict):
                details = {field: entry[field] for field in ("context_window", "max_output_tokens")
                           if isinstance(entry.get(field), int) and entry[field] > 0}
                effort = entry.get("effort", {})
                if isinstance(effort, dict) and isinstance(effort.get("supported_levels"), list):
                    details["effort"] = {"supported_levels": [x for x in effort["supported_levels"]
                        if x in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}]}
                catalog[name.strip()] = details
    if not names:
        raise HTTPException(422, "此账号没有返回可用模型，也可以手动填写模型名称。")
    return sorted(names, key=str.casefold)


class AssetManager:
    """Download pinned assets atomically, verify hashes, and reuse completed files."""

    def __init__(self, root: Path):
        from babeldoc.assets.embedding_assets_metadata import (
            CMAP_METADATA, EMBEDDING_FONT_METADATA, DOC_LAYOUT_ONNX_MODEL_URL,
            DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256, FONT_URL_BY_UPSTREAM,
            CMAP_URL_BY_UPSTREAM, TIKTOKEN_CACHES,
        )
        self.root = root
        self.entries = []
        for folder, metadata, urls in [("fonts", EMBEDDING_FONT_METADATA, FONT_URL_BY_UPSTREAM),
                                        ("cmap", CMAP_METADATA, CMAP_URL_BY_UPSTREAM)]:
            for name, info in metadata.items():
                self.entries.append((folder + "/" + name, info["sha3_256"], info["size"],
                    [urls[upstream](name) for upstream in ("github", "modelscope", "huggingface")]))
        self.entries.append(("models/doclayout_yolo_docstructbench_imgsz1024.onnx",
            DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256, 75324598,
            [DOC_LAYOUT_ONNX_MODEL_URL[x] for x in ("modelscope", "huggingface")]))
        for name, checksum in TIKTOKEN_CACHES.items():
            self.entries.append(("tiktoken/" + name, checksum, 3613922,
                ["https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"]))
        self.total_bytes = sum(entry[2] for entry in self.entries)
        self.state = "idle"
        self.message = "首次使用将下载版式模型和字体"
        self.completed = 0
        self.verified_bytes = 0
        self.inflight: dict[str, int] = {}
        self.task: asyncio.Task | None = None

    def snapshot(self):
        return {"state": self.state, "message": self.message, "completed": self.completed,
                "total": len(self.entries), "bytes": self.verified_bytes + sum(self.inflight.values()),
                "total_bytes": self.total_bytes}

    @staticmethod
    def valid(path, checksum):
        if not path.is_file() or path.is_symlink():
            return False
        digest = hashlib.sha3_256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == checksum

    def start(self):
        if not self.task or self.task.done():
            if self.state != "ready":
                self.task = asyncio.create_task(self.prepare())
        return self.snapshot()

    async def prepare(self):
        self.state, self.message = "checking", "正在校验本地模型与字体…"
        self.completed, self.verified_bytes = 0, 0
        self.inflight.clear()
        try:
            missing = []
            for entry in self.entries:
                if await asyncio.to_thread(self.valid, self.root / entry[0], entry[1]):
                    self.completed += 1
                    self.verified_bytes += entry[2]
                else:
                    missing.append(entry)
            if missing:
                self.state, self.message = "downloading", "正在下载模型与字体，可先配置 API；中断后重试会保留已完成文件。"
                semaphore = asyncio.Semaphore(4)
                async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=15), follow_redirects=True) as client:
                    async def one(entry):
                        async with semaphore:
                            await self.download(client, entry)
                    results = await asyncio.gather(*(one(entry) for entry in missing), return_exceptions=True)
                    if any(isinstance(result, BaseException) for result in results):
                        raise RuntimeError("部分资源下载失败，请检查网络后点击重试。已完成的文件会保留。")
            self.state, self.message = "ready", "模型与字体已就绪"
        except asyncio.CancelledError:
            self.state, self.message = "idle", "准备已中断，可继续下载"
            raise
        except Exception:
            self.state, self.message = "error", "部分资源下载失败，请检查网络后重试；已完成的文件会保留。"

    async def download(self, client, entry):
        name, checksum, size, urls = entry
        destination = self.root / name
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = destination.with_name(destination.name + ".part")
        try:
            for url in urls:
                for _attempt in range(2):
                    self.inflight[name] = 0
                    try:
                        digest = hashlib.sha3_256()
                        async with client.stream("GET", url) as response:
                            response.raise_for_status()
                            with partial.open("wb") as target:
                                async for chunk in response.aiter_bytes(256 * 1024):
                                    self.inflight[name] += len(chunk)
                                    if self.inflight[name] > max(size * 2, size + 4 * 1024 * 1024):
                                        raise ValueError("oversized resource")
                                    target.write(chunk)
                                    digest.update(chunk)
                        if digest.hexdigest() != checksum:
                            raise ValueError("resource checksum mismatch")
                        partial.replace(destination)
                        self.inflight.pop(name, None)
                        self.completed += 1
                        self.verified_bytes += size
                        return
                    except (httpx.HTTPError, ValueError, OSError):
                        partial.unlink(missing_ok=True)
            raise RuntimeError("asset unavailable")
        finally:
            self.inflight.pop(name, None)
            partial.unlink(missing_ok=True)


def install_desktop_routes(worker, web_root: Path, token: str):
    app = worker.app
    app.add_middleware(SessionGuard, token=token)
    app.state.desktop_profile = {}
    app.state.model_catalog = {}
    app.state.assets = AssetManager(worker.ENGINE_CACHE_ROOT)

    @app.post("/api/desktop/profile")
    async def set_profile(request: Request):
        if any(job.status in {"queued", "running", "cancelling"} for job in getattr(worker, "JOBS", {}).values()):
            raise HTTPException(409, "请等待当前任务结束或取消后，再切换 API 配置。")
        profile = await request.json()
        base = provider_url(str(profile.get("base_url", "")), worker.normalize_api_base_url)
        app.state.desktop_profile = {
            "base_url": base, "api_key": str(profile.get("api_key", "")).strip(),
            "model": str(profile.get("model", "")).strip(),
            "qps": str(max(1, min(int(profile.get("qps", 8)), worker.MAX_QPS))),
            "profile_id": str(profile.get("id", "session")),
            "runtime": normalize_options(profile.get("runtime", {})),
        }
        app.state.model_catalog = {}
        return {"ok": True}

    @app.post("/api/provider/models")
    async def list_provider_models(request: Request):
        values = await request.json()
        base = provider_url(str(values.get("base_url", "")), worker.normalize_api_base_url)
        profile = app.state.desktop_profile
        key = str(values.get("api_key", "")).strip()
        if not key and base == profile.get("base_url"):
            key = profile.get("api_key", "")
        if not key:
            raise HTTPException(422, "请先填写此地址的 API 密钥。")
        catalog = {}
        names = await fetch_models(base, key, catalog=catalog)
        if app.state.desktop_profile is profile and base == profile.get("base_url"):
            app.state.model_catalog = catalog
        return {"models": names, "capabilities": catalog}

    @app.get("/api/desktop/assets")
    async def asset_status():
        return app.state.assets.snapshot()

    @app.post("/api/desktop/assets")
    async def prepare_assets():
        return app.state.assets.start()

    @app.on_event("shutdown")
    async def cancel_assets():
        task = app.state.assets.task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # Replace only the desktop root; keep development API behavior intact.
    app.router.routes = [route for route in app.router.routes if getattr(route, "path", None) != "/"]

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(web_root / "index.html", media_type="text/html")

    app.mount("/assets", StaticFiles(directory=web_root / "assets"), name="desktop-assets")
