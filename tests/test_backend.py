import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

try:
    import fitz

    import app
except Exception as exc:  # noqa: BLE001 - test suite reports missing optional dependencies.
    fitz = None
    app = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


@unittest.skipIf(IMPORT_ERROR is not None, f"backend dependencies unavailable: {IMPORT_ERROR}")
class BackendPdfTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source.pdf"
        self.target = self.root / "target.pdf"

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_source(self, *, rotation=0, cropbox=None, link_rect=None):
        document = fitz.open()
        page = document.new_page(width=200, height=100)
        if cropbox is not None:
            page.set_cropbox(cropbox)
        page.set_rotation(rotation)
        page.insert_text((20, 30), "Original text")
        page.insert_link(
            {
                "kind": fitz.LINK_URI,
                "from": link_rect or fitz.Rect(10, 20, 50, 40),
                "uri": "https://example.test/document",
            }
        )
        document.set_metadata({"title": "Original title", "author": "Author"})
        document.set_toc([[1, "Section", 1]])
        document.save(self.source)
        document.close()

    def make_target(self, *, width=200, height=100, rotation=0, cropbox=None):
        document = fitz.open()
        page = document.new_page(width=width, height=height)
        if cropbox is not None:
            page.set_cropbox(cropbox)
        page.set_rotation(rotation)
        page.insert_text((20, 30), "Translated text")
        document.save(self.target)
        document.close()

    @staticmethod
    def link_count(path):
        with fitz.open(path) as document:
            return sum(len(page.get_links()) for page in document)

    def test_mono_link_restore_is_idempotent_and_restores_navigation(self):
        self.make_source()
        self.make_target()

        first = app.restore_links(self.source, self.target)
        second = app.restore_links(self.source, self.target)

        self.assertEqual(first["restored"], 1)
        self.assertEqual(second["restored"], 0)
        self.assertEqual(second["already_present"], 1)
        self.assertEqual(self.link_count(self.target), 1)
        with fitz.open(self.target) as document:
            self.assertEqual(document.metadata["title"], "Original title")
            self.assertEqual(document.get_toc()[0][:3], [1, "Section", 1])

    def test_dual_restore_scales_to_both_panels_without_duplicates(self):
        self.make_source()
        self.make_target(width=400)

        first = app.restore_links_dual(self.source, self.target)
        second = app.restore_links_dual(self.source, self.target)

        self.assertEqual(first["restored"], 2)
        self.assertEqual(second["restored"], 0)
        self.assertEqual(self.link_count(self.target), 2)
        with fitz.open(self.target) as document:
            links = sorted(document[0].get_links(), key=lambda item: item["from"].x0)
            self.assertLess(links[0]["from"].x1, 200)
            self.assertGreaterEqual(links[1]["from"].x0, 200)

    def test_rotated_page_uses_unrotated_insert_coordinates(self):
        self.make_source(rotation=90)
        self.make_target(width=400, height=200, rotation=90)

        app.restore_links(self.source, self.target)

        with fitz.open(self.target) as document:
            link = document[0].get_links()[0]
            self.assertEqual(link["from"], fitz.Rect(120, 20, 160, 100))

    def test_non_default_cropbox_rotation_maps_links_and_stays_idempotent(self):
        source_cropbox = fitz.Rect(20, 10, 180, 90)
        target_cropbox = fitz.Rect(20, 10, 380, 190)
        self.make_source(
            rotation=90,
            cropbox=source_cropbox,
            link_rect=fitz.Rect(30, 20, 70, 40),
        )
        self.make_target(
            width=400,
            height=200,
            rotation=90,
            cropbox=target_cropbox,
        )

        with fitz.open(self.source) as source, fitz.open(self.target) as target:
            source_page = source[0]
            source_link = source_page.get_links()[0]
            target_page = target[0]
            expected = fitz.Rect(
                source_link["from"].x0 * target_page.rect.width / source_page.rect.width,
                source_link["from"].y0 * target_page.rect.height / source_page.rect.height,
                source_link["from"].x1 * target_page.rect.width / source_page.rect.width,
                source_link["from"].y1 * target_page.rect.height / source_page.rect.height,
            )

        first = app.restore_links(self.source, self.target)
        second = app.restore_links(self.source, self.target)

        self.assertEqual(first["restored"], 1)
        self.assertEqual(second["restored"], 0)
        self.assertEqual(second["already_present"], 1)
        with fitz.open(self.target) as document:
            link = document[0].get_links()[0]
            self.assertGreaterEqual(link["from"].x0, 0)
            self.assertGreaterEqual(link["from"].y0, 0)
            self.assertLessEqual(link["from"].x1, document[0].rect.width)
            self.assertLessEqual(link["from"].y1, document[0].rect.height)
            for actual, wanted in zip(link["from"], expected):
                self.assertAlmostEqual(actual, wanted, places=3)

    def test_internal_destination_uses_visual_coordinates_after_rotation(self):
        source_cropbox = fitz.Rect(20, 10, 180, 90)
        target_cropbox = fitz.Rect(20, 10, 380, 190)
        document = fitz.open()
        document.new_page(width=200, height=100)
        document.new_page(width=200, height=100)
        for page_index in range(2):
            document[page_index].set_cropbox(source_cropbox)
            document[page_index].set_rotation(90 if page_index == 0 else 270)
        document[0].insert_link(
            {
                "kind": fitz.LINK_GOTO,
                "from": fitz.Rect(30, 20, 70, 40),
                "page": 1,
                "to": fitz.Point(25, 15),
                "zoom": 0,
            }
        )
        document.save(self.source)
        document.close()

        target_document = fitz.open()
        target_document.new_page(width=400, height=200)
        target_document.new_page(width=400, height=200)
        for page_index in range(2):
            target_document[page_index].set_cropbox(target_cropbox)
            target_document[page_index].set_rotation(90 if page_index == 0 else 270)
        target_document.save(self.target)
        target_document.close()

        with fitz.open(self.source) as source, fitz.open(self.target) as target:
            source_link = source[0].get_links()[0]
            source_page = source[0]
            source_destination = source[1]
            target_page = target[0]
            target_destination = target[1]
            expected_from = fitz.Rect(
                source_link["from"].x0 * target_page.rect.width / source_page.rect.width,
                source_link["from"].y0 * target_page.rect.height / source_page.rect.height,
                source_link["from"].x1 * target_page.rect.width / source_page.rect.width,
                source_link["from"].y1 * target_page.rect.height / source_page.rect.height,
            )
            expected_to = fitz.Point(
                source_link["to"].x * target_destination.rect.width / source_destination.rect.width,
                source_link["to"].y * target_destination.rect.height / source_destination.rect.height,
            )

        result = app.restore_links(self.source, self.target)

        self.assertEqual(result["restored"], 1)
        with fitz.open(self.target) as document:
            link = document[0].get_links()[0]
            for actual, wanted in zip(link["from"], expected_from):
                self.assertAlmostEqual(actual, wanted, places=3)
            self.assertAlmostEqual(link["to"].x, expected_to.x, places=3)
            self.assertAlmostEqual(link["to"].y, expected_to.y, places=3)

    def test_internal_links_with_distinct_destinations_are_not_deduplicated(self):
        document = fitz.open()
        document.new_page(width=200, height=100)
        document.new_page(width=200, height=100)
        document[0].insert_link(
            {
                "kind": fitz.LINK_GOTO,
                "from": fitz.Rect(10, 20, 50, 40),
                "page": 1,
                "to": fitz.Point(20, 20),
                "zoom": 0,
            }
        )
        document[0].insert_link(
            {
                "kind": fitz.LINK_GOTO,
                "from": fitz.Rect(10, 20, 50, 40),
                "page": 1,
                "to": fitz.Point(160, 80),
                "zoom": 0,
            }
        )
        document.save(self.source)
        document.close()
        target_document = fitz.open()
        target_document.new_page(width=200, height=100)
        target_document.new_page(width=200, height=100)
        target_document[0].insert_text((20, 30), "Translated text")
        target_document.save(self.target)
        target_document.close()

        first = app.restore_links(self.source, self.target)
        second = app.restore_links(self.source, self.target)

        self.assertEqual(first["restored"], 2)
        self.assertEqual(second["already_present"], 2)
        self.assertEqual(self.link_count(self.target), 2)

    def test_prompt_leak_is_rejected(self):
        self.make_source()
        document = fitz.open()
        page = document.new_page(width=200, height=100)
        page.insert_text((10, 20), "## Structure Rules\nReturn a JSON array")
        document.save(self.target)
        document.close()

        with self.assertRaisesRegex(ValueError, "指令"):
            app.validate_output_pdf(self.source, self.target)

    def test_prompt_leak_with_control_separators_is_rejected(self):
        self.make_source()
        document = fitz.open()
        page = document.new_page(width=800, height=100)
        page.insert_text(
            (10, 20),
            "Translate\x01the\x01text\x01into\x01Simplified\x01Chinese.\x01"
            "Keep\x01all\x01placeholders,\x01tags,\x01formulas.\x01"
            "Follow\x01all\x01rules\x01strictly.",
        )
        document.save(self.target)
        document.close()

        with self.assertRaisesRegex(ValueError, "指令"):
            app.validate_output_pdf(self.source, self.target)

    def test_prompt_leak_on_late_page_is_rejected(self):
        document = fitz.open()
        for _ in range(7):
            document.new_page(width=200, height=100)
        document.save(self.source)
        document.close()

        document = fitz.open()
        for page_index in range(7):
            page = document.new_page(width=200, height=100)
            if page_index == 6:
                page.insert_text(
                    (10, 20),
                    "Now translate the following text.\n"
                    "Output only the translation.\n"
                    "Follow all rules strictly.",
                )
            else:
                page.insert_text((10, 20), "正常译文")
        document.save(self.target)
        document.close()

        with self.assertRaisesRegex(ValueError, "指令"):
            app.validate_output_pdf(self.source, self.target)

    def test_single_prompt_like_phrase_does_not_trigger_rejection(self):
        self.make_source()
        document = fitz.open()
        page = document.new_page(width=200, height=100)
        page.insert_text((10, 20), "Output only the translation.")
        document.save(self.target)
        document.close()

        app.validate_output_pdf(self.source, self.target)


@unittest.skipIf(IMPORT_ERROR is not None, f"backend dependencies unavailable: {IMPORT_ERROR}")
class BackendConfigTests(unittest.TestCase):
    def test_create_job_preflights_pdf_and_uses_private_tree(self):
        from fastapi.testclient import TestClient

        document = fitz.open()
        document.new_page(width=200, height=100)
        with tempfile.NamedTemporaryFile(suffix=".pdf") as stream:
            document.save(stream.name)
            stream.flush()
            document_bytes = Path(stream.name).read_bytes()
        document.close()

        original_root = app.JOB_ROOT
        original_config = app.CONFIG_PATH
        original_runner = app.run_translation
        original_jobs = app.JOBS
        original_reservations = app.JOB_RESERVATIONS

        async def fake_runner(job, **_kwargs):
            job.status = "completed"
            job.finished = True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app.JOB_ROOT = root / "jobs"
            app.CONFIG_PATH = root / "missing-config.txt"
            app.JOBS = {}
            app.JOB_RESERVATIONS = 0
            app.run_translation = fake_runner
            try:
                with TestClient(app.app) as client:
                    response = client.post(
                        "/api/jobs",
                        files={"file": ("sample.pdf", document_bytes, "application/pdf")},
                        data={
                            "api_base_url": "https://api.example.test",
                            "api_key": "test-key",
                            "model": "test-model",
                            "generate_dual": "false",
                        },
                    )
                self.assertEqual(response.status_code, 202)
                payload = response.json()
                self.assertEqual(payload["page_count"], 1)
                self.assertFalse(payload["dual_requested"])
                job_dir = app.JOB_ROOT / payload["id"]
                self.assertEqual(job_dir.stat().st_mode & 0o777, 0o700)
                self.assertEqual((job_dir / "source.pdf").stat().st_mode & 0o777, 0o600)
            finally:
                app.JOB_ROOT = original_root
                app.CONFIG_PATH = original_config
                app.run_translation = original_runner
                app.JOBS = original_jobs
                app.JOB_RESERVATIONS = original_reservations

    def test_error_redaction(self):
        error = RuntimeError("request failed for secret-token")
        self.assertNotIn("secret-token", app.redact_error(error, "secret-token"))

    def test_cancelled_task_callback_closes_job(self):
        job = app.Job(
            job_id="cancelled-job",
            directory=Path("/tmp/cancelled-job"),
            page_count=1,
            part_pages=None,
            planned_parts=1,
        )

        class CancelledTask:
            @staticmethod
            def cancelled():
                return True

            @staticmethod
            def exception():
                raise asyncio.CancelledError

        app._on_job_task_done(job, CancelledTask())
        self.assertTrue(job.finished)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.events[-1]["type"], "cancelled")

    def test_endpoint_normalization(self):
        self.assertEqual(
            app.normalize_api_base_url("https://api.example.test"),
            "https://api.example.test/v1",
        )
        self.assertEqual(
            app.normalize_api_base_url("https://api.example.test/v1/chat/completions"),
            "https://api.example.test/v1",
        )

    def test_environment_settings_override_file_and_require_model(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "api_config.txt"
            config_path.write_text(
                "base_url=https://file.example.test\napi_key=file-key\n",
                encoding="utf-8",
            )
            with patch.object(app, "CONFIG_PATH", config_path), patch.dict(
                app.os.environ,
                {
                    "PDF_TRANSLATION_API_BASE_URL": "https://env.example.test",
                    "PDF_TRANSLATION_API_KEY": "env-key",
                    "PDF_TRANSLATION_MODEL": "env-model",
                    "PDF_TRANSLATION_MODEL_REASONING_EFFORT": "high",
                },
                clear=False,
            ):
                loaded = app.load_api_config()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["base_url"], "https://env.example.test/v1")
        self.assertEqual(loaded["api_key"], "env-key")
        self.assertEqual(loaded["model"], "env-model")
        self.assertEqual(loaded["model_reasoning_effort"], "high")

    def test_settings_use_direct_endpoint_and_no_watermark(self):
        settings = app.make_settings(
            input_path=Path("/tmp/source.pdf"),
            output_dir=Path("/tmp"),
            api_base_url="https://api.example.test",
            api_key="test-key",
            model="test-model",
            qps=4,
            part_pages=None,
            translate_table_text=False,
            ocr_assist=False,
            reasoning_effort="high",
        )
        self.assertEqual(settings.translate_engine_settings.openai_compatible_base_url, "https://api.example.test/v1")
        self.assertEqual(settings.pdf.watermark_output_mode, "no_watermark")
        self.assertTrue(settings.pdf.no_dual)
        self.assertFalse(settings.pdf.translate_table_text)
        self.assertTrue(settings.translation.ignore_cache)
        self.assertEqual(
            settings.translate_engine_settings.openai_compatible_reasoning_effort,
            "high",
        )
        self.assertTrue(
            settings.translate_engine_settings.openai_compatible_send_reasoning_effort
        )

    def test_rejects_unknown_reasoning_effort(self):
        with self.assertRaisesRegex(ValueError, "模型推理强度"):
            app.make_settings(
                input_path=Path("/tmp/source.pdf"),
                output_dir=Path("/tmp"),
                api_base_url="https://api.example.test",
                api_key="test-key",
                model="test-model",
                qps=1,
                part_pages=None,
                translate_table_text=False,
                ocr_assist=False,
                reasoning_effort="maximum",
            )

    def test_unsupported_table_text_request_is_not_sent_to_upstream(self):
        settings = app.make_settings(
            input_path=Path("/tmp/source.pdf"),
            output_dir=Path("/tmp"),
            api_base_url="https://api.example.test",
            api_key="test-key",
            model="test-model",
            qps=4,
            part_pages=None,
            translate_table_text=True,
            ocr_assist=False,
        )
        self.assertFalse(settings.pdf.translate_table_text)

    def test_openai_compatible_settings_use_installed_openai_adapter(self):
        calls = []

        class FakeCompletions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="Hello")
                        )
                    ],
                    usage=None,
                )

        class FakeClient:
            def __init__(self, **_kwargs):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        settings = app.make_settings(
            input_path=Path("/tmp/source.pdf"),
            output_dir=Path("/tmp"),
            api_base_url="https://api.example.test",
            api_key="test-key",
            model="test-model",
            qps=1,
            part_pages=None,
            translate_table_text=False,
            ocr_assist=False,
        )

        with patch("openai.OpenAI", FakeClient):
            settings.validate_settings()
            from pdf2zh_next.translator.utils import get_translator

            translator = get_translator(settings)

        self.assertEqual(translator.__class__.__name__, "OpenAITranslator")
        self.assertEqual(settings.translate_engine_settings.translate_engine_type, "OpenAI")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "test-model")
        self.assertEqual(calls[0]["messages"][0]["role"], "user")

    def test_run_translation_passes_real_endpoint_without_local_proxy(self):
        captured = []

        async def fake_stream(settings, file):
            captured.append((settings, file))
            result_dir = file.parent / "result"
            result_dir.mkdir(exist_ok=True)
            mono = result_dir / "mono.pdf"
            dual = result_dir / "dual.pdf"
            for path, width in ((mono, 200), (dual, 400)):
                document = fitz.open()
                document.new_page(width=width, height=100)
                document.save(path)
                document.close()
            yield {
                "type": "finish",
                "translate_result": SimpleNamespace(
                    mono_pdf_path=mono,
                    dual_pdf_path=dual,
                    total_seconds=0.1,
                ),
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            document = fitz.open()
            document.new_page(width=200, height=100)
            document.save(source)
            document.close()
            job = app.Job(
                job_id="test-job",
                directory=root,
                page_count=1,
                part_pages=None,
                planned_parts=1,
                dual_requested=True,
            )
            original_stream = app.do_translate_async_stream
            app.do_translate_async_stream = fake_stream
            try:
                import asyncio

                asyncio.run(
                    app.run_translation(
                        job,
                        api_base_url="https://api.example.test/v1",
                        api_key="test-key",
                        model="test-model",
                        qps=2,
                        translate_table_text=False,
                        ocr_assist=False,
                    )
                )
                self.assertTrue(job.output_path and job.output_path.exists())
                self.assertTrue(job.dual_output_path and job.dual_output_path.exists())
            finally:
                app.do_translate_async_stream = original_stream

        self.assertEqual(job.status, "completed")
        self.assertEqual(captured[0][0].translate_engine_settings.openai_compatible_base_url, "https://api.example.test/v1")
        self.assertFalse(captured[0][0].pdf.no_dual)
        self.assertEqual(captured[0][1], source)

    def test_run_translation_link_gap_is_a_downloadable_warning(self):
        async def fake_stream(_settings, file):
            result_dir = file.parent / "result"
            result_dir.mkdir(exist_ok=True)
            mono = result_dir / "mono.pdf"
            document = fitz.open()
            document.new_page(width=200, height=100)
            document.save(mono)
            document.close()
            yield {
                "type": "finish",
                "translate_result": SimpleNamespace(
                    mono_pdf_path=mono,
                    dual_pdf_path=None,
                    total_seconds=0.1,
                ),
            }

        def fake_restore(_source, _target):
            return {"missing": 1, "restore_errors": 1}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            document = fitz.open()
            document.new_page(width=200, height=100)
            document.save(source)
            document.close()
            job = app.Job(
                job_id="link-warning-job",
                directory=root,
                page_count=1,
                part_pages=None,
                planned_parts=1,
            )
            with patch.object(app, "do_translate_async_stream", fake_stream), patch.object(
                app, "restore_links", fake_restore
            ):
                asyncio.run(
                    app.run_translation(
                        job,
                        api_base_url="https://api.example.test/v1",
                        api_key="test-key",
                        model="test-model",
                        qps=2,
                        translate_table_text=False,
                        ocr_assist=False,
                    )
                )

            public = app.public_job(job)
            self.assertEqual(job.status, "completed")
            self.assertTrue(public["download_ready"])
            self.assertIn("仍可下载", job.warnings[0])
            self.assertEqual(job.events[-1]["type"], "completed")
            self.assertEqual(job.events[-1]["warnings"], job.warnings)


if __name__ == "__main__":
    unittest.main()
