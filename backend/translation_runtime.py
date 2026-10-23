"""Version-checked adaptations at the native BabelDOC batch boundary.

The subprocess bootstrap is importable/pickleable in both Python and the frozen
Mac engine. No installed dependency is edited and no JSON batch is rewrapped.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import threading
import time
from collections import Counter
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import httpx

# Spawn imports this module directly, before app.py's startup configuration.
from engine_bootstrap import configure_engine_cache
configure_engine_cache()

from pdf2zh_next.config.model import SettingsModel
from pdf2zh_next import high_level as upstream
from pdf2zh_next.translator.base_rate_limiter import BaseRateLimiter
from pdf2zh_next.translator.translator_impl.openai import OpenAITranslator
from babeldoc.format.pdf import high_level as pdf_pipeline
from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import (
    ILTranslatorLLMOnly, BatchParagraph, is_cid_paragraph, is_pure_numeric_paragraph,
    is_placeholder_only_paragraph,
)
from request_runtime import RequestRuntime, ContextLimitError


class StudioSettings(SettingsModel):
    studio_runtime: dict = {}


_original_wrapper = upstream._translate_wrapper
_original_factory = upstream.create_babeldoc_config
_installed = False
_runtime = None
_placeholder = re.compile(r"\{[^{}]*\}|</?(?:style|b|code)(?:\s[^>]*)?>|\[\[.*?\]\]|%%.*?%%|%[sd]")


class InvalidTranslationResponse(RuntimeError):
    pass


def literal_only(text):
    plain = _placeholder.sub("", text).strip()
    return not plain or bool(re.fullmatch(
        r"(?:https?://\S+|www\.\S+|(?:doi:\s*)?10\.\d{4,9}/\S+|[\d\s.,%+−–=()\[\]/:;-]+|[A-Z][A-Z\d&_. /-]{0,35})", plain, re.I if plain.lower().startswith(("http", "www", "doi:", "10.")) else 0))


def output_issue(source, translated):
    if not isinstance(translated, str) or not translated.strip():
        return "译文为空"
    if Counter(_placeholder.findall(source)) != Counter(_placeholder.findall(translated)):
        return "公式或样式标记不完整"
    for marker in ("## Here is the input:", "## Structure Rules", "Return a JSON array"):
        if marker in translated and marker not in source:
            return "返回了提示词内容"
    if translated.strip() == source.strip() and not literal_only(source):
        # Names and short labels may legitimately remain unchanged.
        if len(source.split()) > 12 and not re.search(r"[\u3400-\u9fff]", source):
            return "正文未翻译"
    return ""


def valid_output(source, translated):
    return not output_issue(source, translated)


def normalize_placeholders(source, translated):
    """Canonicalize equivalent spellings without inventing missing markers."""
    if not isinstance(translated, str):
        return translated
    styles = {m.group(1) for m in re.finditer(r"<style id='(\d+)'>", source)}
    formulas = {m.group(1) for m in re.finditer(r"\{v(\d+)\}", source)}
    translated = re.sub(r"<\s*style\s+id\s*=\s*['\"]\s*(\d+)\s*['\"]\s*>",
        lambda m: f"<style id='{m.group(1)}'>" if m.group(1) in styles else m.group(0),
        translated, flags=re.I)
    if '</style>' in source:
        translated = re.sub(r"<\s*/\s*style\s*>", '</style>', translated, flags=re.I)
    return re.sub(r"\{\s*v\s*(\d+)\s*\}",
        lambda m: '{v'+m.group(1)+'}' if m.group(1) in formulas else m.group(0),
        translated, flags=re.I)


def parse_outputs(text, expected):
    """Return valid IDs and retry IDs, never coerce duplicate IDs into a dict."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    cleaned = re.sub(r"^<json>\s*|\s*</json>$", "", cleaned)
    try:
        rows = json.loads(cleaned)
    except (TypeError, ValueError):
        return {}, set(expected), True
    if isinstance(rows, dict) and "id" in rows:
        rows = [rows]
    if not isinstance(rows, list):
        return {}, set(expected), True
    counts = Counter()
    results = {}
    structural_error = False
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] not in expected:
            structural_error = True
            continue
        id_ = row["id"]
        counts[id_] += 1
        value = normalize_placeholders(expected[id_], row.get("output"))
        if valid_output(expected[id_], value):
            results[id_] = value
    for id_, count in counts.items():
        if count != 1:
            results.pop(id_, None)
    # An extra/unrecognizable ID makes the whole response untrustworthy.
    if structural_error:
        return {}, set(expected), True
    return results, set(expected) - set(results), False


def split_sentence(text):
    protected = [m.span() for m in _placeholder.finditer(text)]
    protected += [m.span() for m in re.finditer(r"https?://\S+|(?:doi:\s*)?10\.\d{4,9}/\S+|\d+(?:[.,]\d+)+", text)]
    middle = len(text) // 2
    def safe(points):
        return [p for p in points if len(text) // 4 < p < 3 * len(text) // 4
                and not any(a < p < b for a, b in protected)]
    points = safe([m.end() for m in re.finditer(r"[.!?。！？;；](?:\s+|(?=[\u3400-\u9fff])|$)", text)])
    if not points:
        points = safe([m.end() for m in re.finditer(r"\s+", text)])
    if not points:
        points = [p for p in range(max(1, middle - 40), min(len(text), middle + 40))
                  if not any(a < p < b for a, b in protected)]
    if not points or len(text) < 24:
        raise RuntimeError("单段文字超过接口限制且无法安全拆分，请选择上下文容量更大的模型。")
    point = min(points, key=lambda p: abs(p - middle))
    return text[:point], text[point:]


class StudioTranslator(OpenAITranslator):
    def __init__(self, settings, runtime):
        super().__init__(settings, BaseRateLimiter())
        self.runtime = runtime
        # Full-document reuse is handled by PaperStudio. Old paragraph caches
        # remain disabled for every path, including native single-block fallback.
        self.ignore_cache = True
        # SDK retries must not multiply the shared five-attempt budget.
        self.client = self.client.with_options(max_retries=0)

    def _call(self, messages):
        response = self.runtime.request(self.client, messages)
        if not response.choices:
            raise InvalidTranslationResponse("接口没有返回译文选项")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise ContextLimitError("译文被接口截断，需要缩小批次。")
        text = choice.message.content
        if not isinstance(text, str):
            raise InvalidTranslationResponse("接口没有返回文本译文")
        return self._remove_cot_content(text).strip()

    def do_llm_translate(self, text, rate_limit_params=None):
        if text is None:
            return None
        return self._call([{"role": "user", "content": text}])

    def do_translate(self, text, rate_limit_params=None):
        messages = [{"role": "system", "content":
            "Translate the supplied paragraph into Simplified Chinese. Return only its translation. "
            "Copy every formula placeholder and every style tag exactly, preserving their numbers, "
            "quotes and spelling. Do not add JSON, markdown fences, explanations or notes."}]
        return self._call(messages + self.prompt(text))


class StudioBatchTranslator(ILTranslatorLLMOnly):
    def __init__(self, translate_engine, translation_config):
        super().__init__(translate_engine, translation_config)
        self.runtime = translate_engine.runtime
        self.errors = []
        self.error_lock = threading.Lock()

    def batch_token_limit(self):
        limit = self.runtime.options["batch_tokens"]
        # Reserve room for the native prompt, JSON and an expanded CJK output.
        context = self.runtime.metadata.get("context_window")
        output = self.runtime.metadata.get("max_output_tokens")
        if isinstance(context, int) and context > 0:
            limit = min(limit, max(128, (context - 1536) // 4))
        if isinstance(output, int) and output > 0:
            limit = min(limit, max(128, output // 3))
        return limit

    def translate(self, docs):
        super().translate(docs)
        if self.errors:
            raise RuntimeError(self.errors[0])

    def process_page(self, page, executor, pbar=None, tracker=None, executor2=None, translated_ids=None):
        # Adapted from BabelDOC 0.6.2 (AGPL-3.0); retain its page/font/ID mapping.
        self.translation_config.raise_if_cancelled()
        font_map = {font.font_id: font for font in page.pdf_font}
        xobj_map = {}
        for xobj in page.pdf_xobject:
            xobj_map[xobj.xobj_id] = {**font_map, **{f.font_id: f for f in xobj.pdf_font}}
        paragraphs, tokens = [], 0
        translated_ids = translated_ids if translated_ids is not None else set()

        def submit():
            if not paragraphs:
                return
            self.mid += 1
            executor.submit(self.translate_paragraph,
                BatchParagraph(list(paragraphs), [page] * len(paragraphs), tracker), pbar,
                font_map, xobj_map,
                self.shared_context_cross_split_part.first_paragraph,
                self.shared_context_cross_split_part.recent_title_paragraph, executor2,
                priority=1048576 - tokens, paragraph_token_count=tokens, mp_id=self.mid)

        for paragraph in page.pdf_paragraph:
            if id(paragraph) in translated_ids or paragraph.debug_id is None or paragraph.unicode is None:
                continue
            if (is_cid_paragraph(paragraph) or len(paragraph.unicode) < self.translation_config.min_text_length
                    or is_pure_numeric_paragraph(paragraph) or is_placeholder_only_paragraph(paragraph)):
                if pbar:
                    pbar.advance(1)
                continue
            count = self.calc_token_count(paragraph.unicode)
            if paragraphs and (tokens + count > self.batch_token_limit()
                               or len(paragraphs) >= self.runtime.options["batch_paragraphs"]):
                submit()
                paragraphs, tokens = [], 0
            if paragraph.layout_label == "title":
                # Do not mix preceding text with a new heading's context.
                submit()
                paragraphs, tokens = [], 0
                self.shared_context_cross_split_part.recent_title_paragraph = self.shared_context_cross_split_part.snapshot_title_paragraph(paragraph)
            paragraphs.append(paragraph)
            translated_ids.add(id(paragraph))
            tokens += count
        submit()

    def resolve(self, entries, title, local_title, *, _split_depth=0):
        # Retries without progress share an envelope, including transport and
        # capability negotiation. Smaller repair tasks get their own envelope.
        with self.runtime.budget():
            return self._resolve(entries, title, local_title, _split_depth)

    def repair_protected(self, item, title, local_title):
        # Translate text spans in place while keeping every original marker.
        # This path reduces the problem to marker-free units and cannot recurse
        # back into itself. It trades sentence-wide reordering for intact math.
        source = item['input']
        pieces, protected, cursor = [], set(), 0
        for match in _placeholder.finditer(source):
            pieces.append(source[cursor:match.start()])
            protected.add(len(pieces))
            pieces.append(match.group())
            cursor = match.end()
        pieces.append(source[cursor:])
        entries = [{**item, 'id':i, 'input':text} for i, text in enumerate(pieces)
                   if i not in protected]
        outputs = {}
        limit = self.runtime.options['batch_paragraphs']
        with self.runtime.condition:
            self.runtime.stats['protected_fallbacks'] += 1
        for offset in range(0, len(entries), limit):
            with self.runtime.budget(fresh=True):
                outputs.update(self.resolve(entries[offset:offset+limit], title, local_title))
        combined = ''.join(text if i in protected else outputs[i] for i, text in enumerate(pieces))
        if not valid_output(source, combined):
            raise RuntimeError("保留公式的分段翻译未通过完整性校验。")
        return {item['id']:combined}

    def _resolve(self, entries, title, local_title, split_depth):
        expected = {item["id"]: item["input"] for item in entries}
        resolved = {id_: text for id_, text in expected.items() if literal_only(text)}
        pending = [item for item in entries if item["id"] not in resolved]
        if not pending:
            return resolved
        requested_count = len(pending)
        prompt = self._build_llm_prompt(json.dumps(pending, ensure_ascii=False, separators=(",", ":")),
            title, local_title, "\n".join(item["input"] for item in pending))
        split = False
        context_limit = False
        try:
            budget = self.runtime.local.budget
            if len(pending) == 1 and budget.attempts >= 3 and _placeholder.search(pending[0]['input']):
                resolved.update(self.repair_protected(pending[0], title, local_title))
                return resolved
            if len(pending) == 1 and budget.attempts >= 2:
                # A provider may translate well while failing the JSON schema.
                # Repair just this paragraph with its plain translation prompt;
                # formula/style validation and the same retry limit still apply.
                item = pending[0]
                with self.runtime.condition:
                    self.runtime.stats["single_fallbacks"] += 1
                response = normalize_placeholders(item['input'], self.translate_engine.translate(item["input"], ignore_cache=True))
                issue = output_issue(item["input"], response)
                try:
                    if isinstance(json.loads(response), (dict, list)):
                        issue = "单段译文仍包含数据包装"
                except (TypeError, ValueError):
                    pass
                if response.lstrip().startswith(("```", "<json>")):
                    issue = "单段译文仍包含数据包装"
                good = {} if issue else {item["id"]: response}
                missing = set(expected) - set(good)
                budget.last_issue = issue
            else:
                response = self.translate_engine.llm_translate(prompt, ignore_cache=True)
                good, missing, split = parse_outputs(response, {item["id"]: item["input"] for item in pending})
                budget.last_issue = "返回格式不符合段落列表" if split else "段落缺失、重复或译文校验未通过"
                # Invalid content also needs smaller repair units, even when
                # the provider returned syntactically valid JSON.
                split = split or not good
            resolved.update(good)
            pending = [item for item in pending if item["id"] in missing]
        except ContextLimitError:
            split = True
            context_limit = True
        except InvalidTranslationResponse as exc:
            split = True
            self.runtime.local.budget.last_issue = str(exc)
        if pending:
            with self.runtime.condition:
                self.runtime.stats["protocol_retries"] += 1
            self.runtime.emit(force=True)

            def child(items, *, depth=split_depth):
                with self.runtime.budget(fresh=True):
                    return self.resolve(items, title, local_title, _split_depth=depth)

            if split and len(pending) > 1:
                middle = len(pending) // 2
                resolved.update(child(pending[:middle]))
                resolved.update(child(pending[middle:]))
            elif context_limit:
                if split_depth >= 8:
                    raise RuntimeError("单段翻译多次拆分后仍超过接口限制，请选择上下文容量更大的模型。")
                item = pending[0]
                left, right = split_sentence(item["input"])
                pieces = child([{**item, "id": 0, "input": left}, {**item, "id": 1, "input": right}], depth=split_depth + 1)
                combined = pieces[0] + pieces[1]
                if not valid_output(item["input"], combined):
                    raise RuntimeError("长段拆分后的公式或样式占位符校验失败。")
                resolved[item["id"]] = combined
            else:
                if len(pending) < requested_count:
                    resolved.update(child(pending))
                else:
                    resolved.update(self.resolve(pending, title, local_title, _split_depth=split_depth))
        return resolved

    def translate_paragraph(self, batch_paragraph, pbar=None, page_font_map=None, xobj_font_map=None,
                            title_paragraph=None, local_title_paragraph=None, executor=None,
                            paragraph_token_count=0, mp_id=0):
        self.translation_config.raise_if_cancelled()
        try:
            prepared = []
            entries = []
            for paragraph, tracker in zip(batch_paragraph.paragraphs, batch_paragraph.trackers):
                text, ti = self.il_translator.pre_translate_paragraph(paragraph, tracker, page_font_map, xobj_font_map)
                if text is None:
                    if pbar:
                        pbar.advance(1)
                    continue
                index = len(prepared)
                tracker.record_multi_paragraph_id(mp_id)
                tracker.record_multi_paragraph_index(index)
                entry = {"id": index, "input": text, "layout_label": paragraph.layout_label}
                hint = ti.get_placeholders_hint()
                if hint and self.translation_config.add_formula_placehold_hint:
                    entry["formula_placeholders_hint"] = hint
                entries.append(entry)
                prepared.append((paragraph, tracker, ti))
            if not prepared:
                return
            with self.runtime.budget():
                outputs = self.resolve(entries, title_paragraph, local_title_paragraph)
            for index, (paragraph, tracker, ti) in enumerate(prepared):
                self.il_translator.post_translate_paragraph(paragraph, tracker, ti, outputs[index])
                with self.runtime.condition:
                    self.runtime.stats["validated_paragraphs"] += 1
                if pbar:
                    pbar.advance(1)
            with self.error_lock:
                self.total_count += len(prepared)
                self.ok_count += len(prepared)
        except Exception as exc:
            # Thread pool futures are not surfaced by the upstream pipeline.
            # Store a safe error and fail the whole document before PDF delivery.
            message = str(exc) if isinstance(exc, RuntimeError) else f"批次处理失败（{exc.__class__.__name__}）。"
            with self.error_lock:
                self.errors.append(message)
            self.runtime.fatal = message
            self.runtime.emit(force=True)


def _create_translator(settings):
    return StudioTranslator(settings, _runtime)


class NativeEnlargedBatchTranslator(ILTranslatorLLMOnly):
    """Benchmark isolation: change page grouping, keep native retry/transport."""
    def __init__(self, translate_engine, translation_config):
        super().__init__(translate_engine, translation_config)
        self.runtime = _runtime

    process_page = StudioBatchTranslator.process_page
    batch_token_limit = StudioBatchTranslator.batch_token_limit


def _create_config(settings, file):
    config = _original_factory(settings, file)
    _runtime.cancel_check = config.raise_if_cancelled
    return config


@contextlib.contextmanager
def observe_legacy_requests(runtime, base_url):
    """Passive measurement for the explicit legacy benchmark path only.

    Keep the pinned batching, SDK retries, Hello checks and thread pools intact;
    count actual HTTP attempts, including the SDK's own retries. No content or
    credentials enter the measurements.
    """
    original = httpx.Client.send
    prefix = base_url.rstrip('/') + '/'
    def send(client, request, *args, **kwargs):
        if not str(request.url).startswith(prefix) or not request.url.path.endswith('/chat/completions'):
            return original(client, request, *args, **kwargs)
        started = time.monotonic()
        with runtime.condition:
            runtime.stats['requests'] += 1
            runtime.stats['in_flight'] += 1
            runtime.stats['peak_in_flight'] = max(runtime.stats['peak_in_flight'], runtime.stats['in_flight'])
            runtime.stats['status'] = 'translating'
        runtime.emit(force=True)
        recorded = False
        try:
            response = original(client, request, *args, **kwargs)
            # Native legacy requests are non-streaming completions. Read once
            # here; HTTPX caches the same bytes for the SDK's normal parser.
            response.read()
            if response.is_success:
                data = response.json()
                usage = data.get('usage') or {}
                details = usage.get('completion_tokens_details') or {}
                usage['completion_tokens_details'] = SimpleNamespace(**details)
                runtime.record_success(SimpleNamespace(usage=SimpleNamespace(**usage)), time.monotonic()-started, {})
                recorded = True
            else:
                with runtime.condition:
                    runtime.stats['last_response_at'] = time.time()
                    if response.status_code == 429: runtime.stats['rate_limits'] += 1
                    elif response.status_code >= 500: runtime.stats['upstream_errors'] += 1
            return response
        finally:
            with runtime.condition:
                runtime.stats['in_flight'] -= 1
                if not recorded:
                    runtime.stats['request_seconds'] += time.monotonic()-started
            runtime.emit(force=True)
    httpx.Client.send = send
    try:
        yield
    finally:
        httpx.Client.send = original


def _studio_wrapper(settings, file, progress, cancel, logger_queue):
    global _runtime
    values = getattr(settings, "studio_runtime", {})
    if values:
        if version("babeldoc") != "0.6.2" or version("pdf2zh-next") != "2.9.0":
            raise RuntimeError("纸间翻译适配器与当前 PDF 引擎版本不匹配，请重新安装完整 App。")
        engine = settings.translate_engine_settings
        _runtime = RequestRuntime(values, stats_path=values.get("stats_path"),
            capability_path=values.get("capability_path"), model=engine.openai_model,
            base_url=engine.openai_base_url, metadata=values.get("model_metadata"))
        if values.get("observe_legacy"):
            if values.get("batch_only"):
                pdf_pipeline.ILTranslatorLLMOnly = NativeEnlargedBatchTranslator
            with observe_legacy_requests(_runtime, engine.openai_base_url):
                try:
                    return _original_wrapper(settings, file, progress, cancel, logger_queue)
                finally:
                    _runtime.stats['status'] = 'finished'
                    _runtime.emit(force=True)
        upstream.get_translator = _create_translator
        # The upstream factory also constructs an unused term translator, whose
        # constructor performs another Hello check even with glossary disabled.
        upstream.get_term_translator = _create_translator
        upstream.create_babeldoc_config = _create_config
        pdf_pipeline.ILTranslatorLLMOnly = StudioBatchTranslator
        _runtime.emit(force=True)
    try:
        return _original_wrapper(settings, file, progress, cancel, logger_queue)
    finally:
        if _runtime:
            _runtime.stats["status"] = "failed" if _runtime.fatal else "finished"
            _runtime.emit(force=True)


def install_runtime():
    global _installed
    if not _installed:
        upstream._translate_wrapper = _studio_wrapper
        _installed = True
