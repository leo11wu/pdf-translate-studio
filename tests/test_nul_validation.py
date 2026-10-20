"""Real PDF fixtures: valid drawings whose Unicode CMap exposes NUL."""
import asyncio
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import app


class NulValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.pdf"
        self.output = self.root / "output.pdf"

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, path, *, null=True, text="A", font="cjk", x=60, duplicate=False, leak=False):
        document = fitz.open()
        page = document.new_page()
        fixture_font = fitz.Font(font)
        page.insert_font(fontname="Fixture", fontbuffer=fixture_font.buffer)
        page.insert_text((x, 60), text, fontname="Fixture")
        if duplicate:
            page.insert_text((x + 30, 60), text, fontname="Fixture")
        if null:
            xref = page.get_fonts()[0][0]
            cmap = int(document.xref_get_key(xref, "ToUnicode")[1].split()[0])
            glyph = fixture_font.has_glyph(ord(text))
            descendant = int(re.search(r"(\d+) 0 R", document.xref_get_key(xref, "DescendantFonts")[1])[1])
            if document.xref_get_key(descendant, "Subtype")[1] == "/CIDFontType2":
                mapping = document.get_new_xref()
                document.update_object(mapping, "<<>>")
                document.update_stream(mapping, glyph.to_bytes(2, "big"))
                document.xref_set_key(descendant, "CIDToGIDMap", f"{mapping} 0 R")
            for content in page.get_contents():
                stream = document.xref_stream(content)
                document.update_stream(content, stream.replace(f"<{glyph:04x}>".encode(), b"<0000>"))
            document.update_stream(cmap, (
                "/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
                "/CIDSystemInfo <</Registry(Adobe)/Ordering(UCS)/Supplement 0>> def "
                "/CMapName /Fixture def /CMapType 2 def "
                "1 begincodespacerange <0000> <FFFF> endcodespacerange "
                "1 beginbfchar <0000> <0000> endbfchar "
                "endcmap CMapName currentdict /CMap defineresource pop end end"
            ).encode())
        if leak:
            page.insert_text((20, 100), "## Structure Rules\nReturn a JSON array")
        document.save(path)
        document.close()
        if null:
            with fitz.open(path) as check:
                self.assertIn("\0", check[0].get_text())

    def test_unchanged_embedded_source_glyph_is_allowed(self):
        self.fixture(self.source)
        self.fixture(self.output)
        stats = app.validate_output_pdf(self.source, self.output)
        self.assertEqual(stats, {"inherited_nul_glyphs": 1, "nul_pages": [1]})

    def test_two_dual_panels_each_preserve_the_source_glyph(self):
        self.fixture(self.source)
        with fitz.open(self.source) as source, fitz.open() as output:
            rect = source[0].rect
            page = output.new_page(width=rect.width * 2, height=rect.height)
            page.show_pdf_page(rect, source, 0)
            page.show_pdf_page(rect + (rect.width, 0, rect.width, 0), source, 0)
            output.save(self.output)
        self.assertEqual(app.validate_output_pdf(self.source, self.output)["inherited_nul_glyphs"], 2)

    def test_new_null_with_clean_source_is_blocked(self):
        self.fixture(self.source, null=False)
        self.fixture(self.output)
        with self.assertRaisesRegex(ValueError, "NUL"):
            app.validate_output_pdf(self.source, self.output)

    def test_changed_font_glyph_position_or_duplicate_is_blocked(self):
        self.fixture(self.source)
        for settings in ({"font": "tiro"}, {"text": "B"}, {"x": 75}, {"duplicate": True}):
            with self.subTest(settings=settings):
                self.fixture(self.output, **settings)
                with self.assertRaisesRegex(ValueError, "NUL"):
                    app.validate_output_pdf(self.source, self.output)

    def test_inherited_null_does_not_bypass_prompt_checks(self):
        self.fixture(self.source)
        self.fixture(self.output, leak=True)
        with self.assertRaisesRegex(ValueError, "指令"):
            app.validate_output_pdf(self.source, self.output)

    def test_legacy_failure_recovery_generates_txt_without_api(self):
        result = self.root / "result"
        result.mkdir()
        self.fixture(self.source)
        self.fixture(result / "source.mono.pdf")
        job = app.Job(job_id="f" * 32, directory=self.root, page_count=1, part_pages=None,
                      planned_parts=1, status="failed", finished=True, finished_at=100,
                      error_message="输出 PDF 文本包含 NUL 字符，已阻止交付")
        app.persist_job(job)
        previous = app.JOBS
        app.JOBS = {job.job_id: job}
        try:
            asyncio.run(app.recover_nul_failed_jobs())
        finally:
            app.JOBS = previous
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.finished_at, 100)
        self.assertTrue(job.output_path.is_file())
        self.assertNotIn("\0", job.translated_text_path.read_text())
        self.assertEqual(json.loads((self.root / "record.before-nul-recovery.json").read_text())["status"], "failed")
        self.assertTrue(any("没有再次调用" in w for w in job.warnings))

    def test_corrupt_legacy_failure_is_not_recovered(self):
        result = self.root / "result"
        result.mkdir()
        self.fixture(self.source, null=False)
        self.fixture(result / "source.mono.pdf")
        job = app.Job(job_id="f" * 32, directory=self.root, page_count=1, part_pages=None,
                      planned_parts=1, status="failed", finished=True,
                      error_message="输出 PDF 文本包含 NUL 字符，已阻止交付")
        app.persist_job(job)
        previous = app.JOBS
        app.JOBS = {job.job_id: job}
        try:
            asyncio.run(app.recover_nul_failed_jobs())
        finally:
            app.JOBS = previous
        self.assertEqual(job.status, "failed")
        self.assertIsNone(job.output_path)


if __name__ == "__main__":
    unittest.main()
