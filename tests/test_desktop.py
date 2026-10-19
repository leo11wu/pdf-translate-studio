import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import httpx
from fastapi import FastAPI, HTTPException
import app as worker
from desktop_support import AssetManager, SessionGuard, fetch_models, install_desktop_routes, provider_url


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, payload=None, status=200, headers=None):
        seen = []
        def respond(request):
            seen.append(request)
            return httpx.Response(status, json=payload, headers=headers)
        original = httpx.AsyncClient
        with patch("desktop_support.httpx.AsyncClient", side_effect=lambda **kwargs: original(
            transport=httpx.MockTransport(respond), **kwargs)):
            names = await fetch_models("https://provider.test/custom/v1", "test-secret")
        return names, seen

    async def test_model_list_normalizes_and_preserves_custom_path(self):
        names, seen = await self.request({"data": [{"id": "z-model"}, {"id": "a-model"}, {"id": "z-model"}]})
        self.assertEqual(names, ["a-model", "z-model"])
        self.assertEqual(str(seen[0].url), "https://provider.test/custom/v1/models")
        self.assertEqual(seen[0].headers["Authorization"], "Bearer test-secret")

    async def test_no_credentials_follow_redirect(self):
        with self.assertRaises(HTTPException) as raised:
            await self.request(status=302, headers={"Location": "https://other.test/models"})
        self.assertIn("重定向", raised.exception.detail)

    async def test_provider_errors_do_not_echo_secrets(self):
        for status in (401, 404, 429, 500):
            with self.subTest(status=status), self.assertRaises(HTTPException) as raised:
                await self.request({"detail": "test-secret"}, status=status)
            self.assertNotIn("test-secret", raised.exception.detail)

    def test_provider_url_rejects_embedded_credentials_and_query(self):
        for value in ("https://key@provider.test", "https://provider.test?api_key=secret", "file:///etc/passwd", "http://localhost:bad"):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                provider_url(value, worker.normalize_api_base_url)
        self.assertEqual(provider_url("https://provider.test/v2/chat/completions/", worker.normalize_api_base_url), "https://provider.test/v2")


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.add_middleware(SessionGuard, token="session-test-token")
        @self.app.get("/events")
        async def example():
            return {"ok": True}
        @self.app.post("/api/desktop/profile")
        async def profile():
            return {"ok": True}
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:45678")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_session_required_and_foreign_origin_rejected(self):
        self.assertEqual((await self.client.get("/events")).status_code, 403)
        headers = {"Cookie": "studio_session=session-test-token"}
        response = await self.client.get("/events", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        for values in ({"Origin": "https://foreign.test"}, {"Host": "foreign.test:45678"}):
            response = await self.client.get("/events", headers={**headers, **values})
            self.assertEqual(response.status_code, 403)

    async def test_profile_is_native_bearer_only(self):
        response = await self.client.post("/api/desktop/profile", headers={"Cookie": "studio_session=session-test-token"})
        self.assertEqual(response.status_code, 403)
        response = await self.client.post("/api/desktop/profile", headers={"Authorization": "Bearer session-test-token"})
        self.assertEqual(response.status_code, 200)

    def test_desktop_never_reads_developer_config(self):
        with patch.dict(os.environ, {"PDF_STUDIO_DESKTOP": "1", "PDF_TRANSLATION_API_KEY": "developer-secret"}), \
             patch.object(worker.app.state, "desktop_profile", {}, create=True):
            self.assertIsNone(worker.load_api_config())

    async def test_saved_key_is_bound_to_exact_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "assets").mkdir()
            (root / "index.html").write_text("test")
            test_worker = type("Worker", (), {"app": FastAPI(), "ENGINE_CACHE_ROOT": root / "cache", "MAX_QPS": 64,
                "normalize_api_base_url": staticmethod(worker.normalize_api_base_url)})
            install_desktop_routes(test_worker, root, "session-test-token")
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=test_worker.app), base_url="http://127.0.0.1:45678",
                headers={"Authorization": "Bearer session-test-token"})
            async with client:
                await client.post("/api/desktop/profile", json={"base_url": "https://one.test", "model": "example", "api_key": "saved-secret"})
                with patch("desktop_support.fetch_models", return_value=["example"]) as fetch:
                    response = await client.post("/api/provider/models", json={"base_url": "https://one.test", "api_key": ""})
                    self.assertEqual(response.status_code, 200)
                    fetch.assert_awaited_once_with("https://one.test/v1", "saved-secret")
                    response = await client.post("/api/provider/models", json={"base_url": "https://two.test", "api_key": ""})
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(fetch.await_count, 1)


class AssetTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_hash_check_and_retry_reuses_completed_files(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = AssetManager(Path(temp))
            contents = b"valid-model-file"
            checksum = hashlib.sha3_256(contents).hexdigest()
            manager.entries = [("models/example.onnx", checksum, len(contents), ["https://assets.test/model"])]
            calls = []
            def respond(request):
                calls.append(request)
                return httpx.Response(200, content=contents)
            original = httpx.AsyncClient
            with patch("desktop_support.httpx.AsyncClient", side_effect=lambda **kwargs: original(
                transport=httpx.MockTransport(respond), **kwargs)):
                await manager.prepare()
                self.assertEqual(manager.state, "ready")
                self.assertEqual(len(calls), 1)
                await manager.prepare()
                self.assertEqual(len(calls), 1)
            self.assertEqual((Path(temp) / manager.entries[0][0]).read_bytes(), contents)
            self.assertEqual(list(Path(temp).rglob("*.part")), [])

    async def test_corrupt_download_is_not_promoted(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = AssetManager(Path(temp))
            entry = ("models/example.onnx", hashlib.sha3_256(b"expected").hexdigest(), 8, ["https://assets.test/model"])
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"wrong"))) as client:
                with self.assertRaises(RuntimeError):
                    await manager.download(client, entry)
            self.assertFalse((Path(temp) / entry[0]).exists())
            self.assertEqual(list(Path(temp).rglob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
