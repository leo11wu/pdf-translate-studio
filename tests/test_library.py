import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import fitz
import httpx
import app as worker
from library import extract_text, fingerprint, safe_filename, write_record


def pdf_bytes(text="Language research, 679 words and 341 items."):
    with fitz.open() as document:
        document.new_page().insert_text((60, 90), text)
        return document.tobytes()


class TextTests(unittest.TestCase):
    def test_columns_pages_and_empty_pages_without_inventing_text(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / "input.pdf", Path(temp) / "source.txt"
            with fitz.open() as document:
                page = document.new_page(width=600, height=800)
                for x, y, text in [(320, 100, "Right one"), (60, 100, "Left one"),
                                   (320, 140, "Right two - 341"), (60, 140, "Left two - 679")]:
                    page.insert_text((x, y), text)
                document.new_page()
                document.save(source)
            result = extract_text(source, target)
            text = target.read_text()
            self.assertLess(text.index("Left two"), text.index("Right one"))
            self.assertIn("第 2 页 / 共 2 页", text)
            self.assertEqual(result["empty_pages"], [2])
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("\x00", text)

    def test_filename_and_cache_profile_isolation(self):
        self.assertEqual(safe_filename("../../report.pdf"), "report.pdf")
        self.assertNotIn("/", safe_filename("a\\b/../../x\x00.pdf"))
        self.assertLess(len(safe_filename("研究" * 150 + ".pdf").encode()), 180)
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            source.write_bytes(pdf_bytes())
            first = fingerprint(source, provider="https://one.test/v1", model="model-a", prompt="prompt")
            for settings in [dict(provider="https://two.test/v1", model="model-a", prompt="prompt"),
                             dict(provider="https://one.test/v1", model="model-b", prompt="prompt"),
                             dict(provider="https://one.test/v1", model="model-a", prompt="new prompt")]:
                self.assertNotEqual(first, fingerprint(source, **settings))


class LibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input_pdf = pdf_bytes()
        self.patches = [patch.object(worker, "JOB_ROOT", self.root), patch.object(worker, "JOBS", {}),
                        patch.object(worker, "load_api_config", return_value=None),
                        patch.dict(os.environ, {"PDF_STUDIO_DESKTOP": "1"})]
        for item in self.patches:
            item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="http://127.0.0.1:45678")

    async def asyncTearDown(self):
        for job in worker.JOBS.values():
            if job.task and not job.task.done():
                job.task.cancel()
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def submit(self, data=None):
        response = await self.client.post("/api/jobs", files={"file": ("research.pdf", self.input_pdf, "application/pdf")}, data=data or {})
        self.assertEqual(response.status_code, 202, response.text)
        job = worker.JOBS[response.json()["id"]]
        await job.task
        return job

    async def test_text_only_needs_no_key_persists_and_restores_download(self):
        with patch.object(worker, "do_translate_async_stream") as engine:
            job = await self.submit({"extract_only": "true"})
        engine.assert_not_called()
        self.assertEqual(job.status, "completed")
        download = await self.client.get(f"/api/jobs/{job.job_id}/source.txt")
        self.assertIn("679 words", download.text)
        self.assertIn("charset=utf-8", download.headers["content-type"])
        self.assertEqual((await self.client.get(f"/api/jobs/{job.job_id}/translated.txt")).status_code, 404)
        record = job.directory / "record.json"
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        worker.JOBS.clear()
        worker.restore_history()
        history = (await self.client.get("/api/history")).json()
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["jobs"][0]["status"], "completed")
        self.assertEqual((await self.client.get(f"/api/jobs/{job.job_id}/source.txt")).content, download.content)
        with patch.object(worker, "JOB_TTL_SECONDS", -1):
            worker.cleanup_expired_jobs()
        self.assertTrue(record.exists())

    async def fake_engine(self, settings, input_path):
        path = Path(settings.translation.output) / "output.mono.pdf"
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((60, 90), "语言学习研究 679 341", fontname="china-s")
            document.save(path)
        yield {"type": "progress_update", "stage": "Translate Paragraphs", "overall_progress": 80}
        yield {"type": "finish", "translate_result": SimpleNamespace(mono_pdf_path=path, dual_pdf_path=None)}

    async def test_verified_reuse_bypasses_engine_and_retains_text_artifacts(self):
        config = {"api_base_url": "https://one.test/v1", "api_key": "do-not-save-this-secret", "model": "model-a", "qps": "8"}
        with patch.object(worker, "do_translate_async_stream", side_effect=self.fake_engine) as engine:
            first = await self.submit(config)
            self.assertEqual(first.status, "completed", first.events[-1])
            self.assertEqual(len(worker.job_artifacts(first)), 3)
            second = await self.submit({**config, "qps": "16"})
            self.assertTrue(second.cache_hit)
            self.assertEqual(engine.call_count, 1)
            self.assertEqual(first.output_path.read_bytes(), second.output_path.read_bytes())
            self.assertIn("语言学习", second.translated_text_path.read_text())
            worker.JOBS.clear()
            worker.restore_history()
            restored_reuse = await self.submit(config)
            self.assertTrue(restored_reuse.cache_hit)
            self.assertEqual(engine.call_count, 1)
            third = await self.submit({**config, "use_cache": "false", "export_texts": "false"})
            self.assertFalse(third.cache_hit)
            self.assertEqual(engine.call_count, 2)
            self.assertEqual(len(worker.job_artifacts(third)), 1)
            extra_text = await self.client.post(f"/api/jobs/{third.job_id}/prepare-text")
            self.assertEqual(extra_text.status_code, 200, extra_text.text)
            self.assertEqual(len(extra_text.json()["artifacts"]), 3)
            self.assertEqual(engine.call_count, 2)
            fourth = await self.submit({**config, "model": "model-b"})
            self.assertFalse(fourth.cache_hit)
            self.assertEqual(engine.call_count, 3)
        for record in self.root.glob("*/record.json"):
            self.assertNotIn("do-not-save-this-secret", record.read_text())
        self.assertEqual((await self.client.get("/api/history?q=model-b")).json()["total"], 1)

    async def test_interrupted_restart_and_manifest_cannot_escape_job(self):
        directory = self.root / ("a" * 32)
        directory.mkdir()
        (directory / "source.pdf").write_bytes(pdf_bytes())
        write_record(directory, {"schema": 1, "id": directory.name, "page_count": 1, "status": "running",
                                 "filename": "research.pdf", "files": {"output_path": "../../outside.pdf"}})
        worker.restore_history()
        job = worker.JOBS[directory.name]
        self.assertEqual(job.status, "interrupted")
        self.assertTrue(job.finished)
        self.assertIsNone(job.output_path)
        self.assertEqual(worker.job_artifacts(job), [])
        self.assertEqual((await self.client.get("/api/jobs/not-a-job/source.txt")).status_code, 404)


if __name__ == "__main__":
    unittest.main()
