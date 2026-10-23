import concurrent.futures
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import app  # Configure the writable engine cache before importing upstream code.
from request_runtime import RequestRuntime, RequestBudget, capability_candidates, parameter_rejection, ContextLimitError, normalize_options
from translation_runtime import parse_outputs, valid_output, literal_only, split_sentence, StudioBatchTranslator, normalize_placeholders


class FakeError(Exception):
    def __init__(self, status, message="error", retry_after="0"):
        super().__init__(message)
        self.status_code = status
        self.response = SimpleNamespace(headers={"retry-after": retry_after})


def response(text="译文"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10, total_tokens=30,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0)))


def client(fn):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fn)))


class ProtocolTests(unittest.TestCase):
    def test_ids_and_placeholders_are_not_silently_dropped(self):
        source = {0: "A result {v1}", 1: "Language learning is complex."}
        good, missing, malformed = parse_outputs('[{"id":1,"output":"语言学习很复杂。"},{"id":0,"output":"结果 {v1}"}]', source)
        self.assertEqual(set(good), {0, 1})
        self.assertFalse(missing or malformed)
        for text, expected_missing in [('[{"id":0,"output":"结果 {v1}"}]', {1}),
            ('[{"id":0,"output":"结果"},{"id":1,"output":"学习"}]', {0}),
            ('[{"id":0,"output":"结果 {v1}"},{"id":0,"output":"另一个 {v1}"}]', {0, 1})]:
            self.assertEqual(parse_outputs(text, source)[1], expected_missing)
        for text in ('broken', '[{"id":-1,"output":"错误"}]', '[{"id":true,"output":"错误"}]'):
            self.assertTrue(parse_outputs(text, source)[2])

    def test_literal_passthrough_and_prompt_leak(self):
        for text in ('10.1000/182', 'https://example.com/a', 'PDF', '{v1}', '2026 (4)'):
            self.assertTrue(literal_only(text), text)
        self.assertFalse(literal_only('Researchers found a significant relationship.'))
        self.assertFalse(valid_output('Text {v1}', '文本 {v2}'))
        self.assertFalse(valid_output('Text', '## Here is the input: Text'))
        self.assertTrue(valid_output("<style id='1'>A</style> {v2}", "<style id='1'>甲</style> {v2}"))

    def test_equivalent_marker_spellings_are_canonicalized_but_missing_markers_still_fail(self):
        source = "A <style id='1'>result</style> {v2}"
        canonical = normalize_placeholders(source, '一项 <STYLE id="1">结果< /style > { V 2 }')
        self.assertEqual(canonical, "一项 <style id='1'>结果</style> {v2}")
        self.assertTrue(valid_output(source, canonical))
        self.assertFalse(valid_output(source, normalize_placeholders(source, "一项结果 {v2}")))
        self.assertFalse(valid_output(source, normalize_placeholders(source, "一项 <style id='9'>结果</style> {v2}")))

    def test_split_keeps_all_characters_and_placeholder_tokens(self):
        source = 'First sentence with {v1}. Second sentence with {v2}. Third sentence with figures 35.2%.'
        left, right = split_sentence(source)
        self.assertEqual(left + right, source)
        self.assertNotIn('{v', left[-2:])
        source = 'Short intro. ' + '35.21234 ' * 10 + 'https://example.test/long.file?q=10.33 ' + 'Second sentence. ' * 5
        left, right = split_sentence(source)
        self.assertEqual(left + right, source)
        self.assertFalse(left[-1].isdigit() and right[0].isdigit())

    def resolver(self, answer):
        runtime = RequestRuntime({'reasoning_policy':'provider_default', 'request_rate':64}, model='test')
        batch = StudioBatchTranslator.__new__(StudioBatchTranslator)
        batch.runtime = runtime
        batch._build_llm_prompt = lambda source, *_: source
        def call(**kwargs):
            entries = json.loads(kwargs['messages'][0]['content'])
            return response(answer(entries))
        batch.translate_engine = SimpleNamespace(llm_translate=lambda text, **_: runtime.request(client(call), [{'role':'user','content':text}]).choices[0].message.content)
        batch.translate_engine.translate = lambda text, **_: runtime.request(
            client(lambda **_: response('[]')), [{'role':'user','content':text}]).choices[0].message.content
        return batch

    def test_single_paragraph_fallback_repairs_json_but_keeps_placeholders(self):
        batch = self.resolver(lambda _: 'not JSON')
        def plain(text, **_):
            return batch.runtime.request(client(lambda **_: response('有效译文 {v1}')), []).choices[0].message.content
        batch.translate_engine.translate = plain
        result = batch.resolve([{'id':7,'input':'Research sentence {v1}.'}], None, None)
        self.assertEqual(result, {7:'有效译文 {v1}'})
        self.assertEqual(batch.runtime.stats['requests'], 3)
        self.assertEqual(batch.runtime.stats['single_fallbacks'], 1)

    def test_invalid_content_is_bisected_and_plain_fallback_cannot_drop_formulas(self):
        batch = self.resolver(lambda _: '[]')
        def plain(text, **_):
            return batch.runtime.request(client(lambda **_: response('丢失公式的译文')), []).choices[0].message.content
        batch.translate_engine.translate = plain
        result = batch.resolve([{'id':7,'input':'Research sentence {v1}.'}], None, None)
        self.assertTrue(valid_output('Research sentence {v1}.', result[7]))
        self.assertIn('{v1}', result[7])
        self.assertEqual(batch.runtime.stats['protected_fallbacks'], 1)

    def test_protected_repair_still_fails_when_text_spans_never_translate(self):
        batch = self.resolver(lambda _: '[]')
        with self.assertRaisesRegex(RuntimeError, '5 次'):
            batch.resolve([{'id':7,'input':"A <style id='1'>research result</style> {v1}."}], None, None)
        self.assertEqual(batch.runtime.stats['protected_fallbacks'], 1)
        self.assertLessEqual(batch.runtime.stats['requests'], 8)

    def test_partial_failure_retries_only_missing_or_damaged_paragraph(self):
        seen = []
        def answer(entries):
            seen.append([item['id'] for item in entries])
            return json.dumps([{'id':item['id'], 'output':'译文 ' + ('{v1}' if '{v1}' in item['input'] else '')}
                for item in entries if len(seen) > 1 or item['id'] == 1])
        batch = self.resolver(answer)
        result = batch.resolve([{'id':0,'input':'Text {v1}'},{'id':1,'input':'Other text'}], None, None)
        self.assertEqual(set(result), {0,1})
        self.assertEqual(seen, [[0,1], [0]])

    def test_malformed_batch_is_bisected_and_literals_do_not_call_provider(self):
        seen = []
        def answer(entries):
            seen.append(len(entries))
            if len(entries) > 2: return 'not JSON'
            return json.dumps([{'id':item['id'], 'output':'有效译文'} for item in reversed(entries)])
        batch = self.resolver(answer)
        entries = [{'id':i,'input':f'Research paragraph {i}'} for i in range(4)]
        entries.append({'id':4,'input':'https://example.test'})
        with batch.runtime.budget(): result = batch.resolve(entries, None, None)
        self.assertEqual(seen, [4,2,2])
        self.assertEqual(result[4], 'https://example.test')
        self.assertEqual(batch.runtime.stats['requests'], 3)

    def test_split_children_do_not_exhaust_successful_siblings_budget(self):
        seen = []
        def answer(entries):
            seen.append(len(entries))
            if len(entries) > 1:
                return 'not JSON'
            return json.dumps([{'id':entries[0]['id'], 'output':'有效译文'}])
        batch = self.resolver(answer)
        entries = [{'id':i, 'input':f'Research paragraph {i}'} for i in range(8)]
        with batch.runtime.budget():
            result = batch.resolve(entries, None, None)
        self.assertEqual(set(result), set(range(8)))
        self.assertEqual(len(seen), 15)

    def test_partial_progress_gets_a_budget_for_the_smaller_remainder(self):
        seen = []
        def answer(entries):
            seen.append(len(entries))
            return json.dumps([{'id':entries[0]['id'], 'output':'有效译文'}])
        batch = self.resolver(answer)
        entries = [{'id':i, 'input':f'Research paragraph {i}'} for i in range(8)]
        with batch.runtime.budget():
            result = batch.resolve(entries, None, None)
        self.assertEqual(set(result), set(range(8)))
        self.assertEqual(seen, list(range(8, 0, -1)))

    def test_truncation_splits_a_single_paragraph_and_protocol_failure_is_bounded(self):
        batch = self.resolver(lambda entries: json.dumps([{'id':x['id'],'output':'译文。'} for x in entries]))
        original = batch.translate_engine.llm_translate
        calls = []
        def truncated(text, **kwargs):
            calls.append(text)
            if len(calls) == 1: raise ContextLimitError('truncated')
            return original(text, **kwargs)
        batch.translate_engine.llm_translate = truncated
        result = batch.resolve([{'id':7,'input':'A first research sentence. A second research sentence. A third sentence.'}], None, None)
        self.assertEqual(result[7], '译文。译文。')
        broken = self.resolver(lambda _: '[{"id":0,"output":false}]')
        with broken.runtime.budget(), self.assertRaisesRegex(RuntimeError, '5 次'):
            broken.resolve([{'id':0,'input':'Research sentence.'}], None, None)
        broken = self.resolver(lambda _: 'not JSON')
        with self.assertRaisesRegex(RuntimeError, '5 次'):
            broken.resolve([{'id':0,'input':'Research sentence.'}], None, None)
        self.assertEqual(broken.runtime.stats['requests'], 5)

    def test_bootstrap_bypasses_both_upstream_hello_factories(self):
        import app
        import translation_runtime as adapter
        with tempfile.TemporaryDirectory() as temp:
            settings = app.make_settings(input_path=Path(temp)/'test.pdf', output_dir=Path(temp),
                api_base_url='https://provider.test/v1', api_key='fixture-key', model='test', qps=4,
                part_pages=None, translate_table_text=False, ocr_assist=False,
                runtime_options=normalize_options())
            settings.validate_settings()
            config = SimpleNamespace(raise_if_cancelled=lambda: None)
            with patch.object(adapter.upstream, 'get_translator'), patch.object(adapter.upstream, 'get_term_translator'), \
                 patch.object(adapter.upstream, 'create_babeldoc_config'), patch.object(adapter.pdf_pipeline, 'ILTranslatorLLMOnly'), \
                 patch.object(adapter, '_runtime', None), patch.object(adapter, 'StudioTranslator') as constructor, \
                 patch.object(adapter.upstream, 'BabelDOCConfig', return_value=config), \
                 patch('pdf2zh_next.translator.utils._create_translator_instance', side_effect=AssertionError('Hello factory called')), \
                 patch.object(adapter, '_original_wrapper', side_effect=lambda s,f,*_: adapter._create_config(s,f)):
                adapter._studio_wrapper(settings, Path(temp)/'test.pdf', None, None, None)
                self.assertGreaterEqual(constructor.call_count, 1)


class TransportTests(unittest.TestCase):
    def make(self, **overrides):
        return RequestRuntime({"concurrency_mode": "manual", "max_in_flight": 4,
            "request_rate": 64, "reasoning_policy": "provider_default", **overrides}, model="test")

    def test_one_budget_for_transport_retries(self):
        runtime = self.make()
        runtime.pause = lambda *_: None
        count = 0
        def fail(**kwargs):
            nonlocal count
            count += 1
            raise FakeError(502)
        with self.assertRaisesRegex(RuntimeError, '5 次'):
            runtime.request(client(fail), [])
        self.assertEqual(count, 5)
        self.assertEqual(runtime.stats['upstream_errors'], 5)
        self.assertEqual(runtime.stats['in_flight'], 0)

    def test_scheduler_wait_does_not_use_response_time_budget(self):
        runtime = self.make()
        clock = [1000.0]
        observed = []
        with patch('request_runtime.time.monotonic', side_effect=lambda: clock[0]):
            runtime.next_request = clock[0] + 181
            def wait(seconds):
                clock[0] += seconds
            def create(**kwargs):
                observed.append(kwargs['timeout'])
                return response()
            with patch.object(runtime.condition, 'wait', side_effect=wait):
                runtime.request(client(create), [])
        self.assertEqual(observed, [120])
        self.assertEqual(runtime.stats['successful_requests'], 1)

    def test_response_time_budget_still_bounds_slow_requests(self):
        runtime = self.make()
        runtime.pause = lambda *_: None
        clock = [1000.0]
        observed = []
        with patch('request_runtime.time.monotonic', side_effect=lambda: clock[0]):
            def fail(**kwargs):
                observed.append(kwargs['timeout'])
                clock[0] += kwargs['timeout']
                raise FakeError(504)
            with self.assertRaisesRegex(RuntimeError, '180 秒'):
                runtime.request(client(fail), [])
        self.assertEqual(observed, [120, 60])
        self.assertEqual(runtime.stats['in_flight'], 0)

    def test_capability_queue_does_not_use_response_time_budget(self):
        runtime = self.make(reasoning_policy='lowest')
        clock = [1000.0]
        acquisitions = []
        def acquire(**_):
            acquisitions.append(True)
            if len(acquisitions) == 1:
                clock[0] += 181
                return False
            return True
        runtime.negotiation = SimpleNamespace(acquire=acquire, release=lambda:None)
        observed = []
        def create(**kwargs):
            observed.append(kwargs['timeout'])
            return response()
        with patch('request_runtime.time.monotonic', side_effect=lambda: clock[0]):
            runtime.request(client(create), [])
        self.assertEqual(observed, [120])

    def test_finite_capability_probes_leave_room_for_content_repair(self):
        runtime = RequestRuntime({'reasoning_policy':'lowest','request_rate':64}, model='deepseek-flash')
        def create(**kwargs):
            if 'reasoning_effort' in kwargs or 'extra_body' in kwargs:
                raise FakeError(400, 'Unsupported reasoning_effort and thinking parameter')
            return response()
        with runtime.budget() as budget:
            runtime.request(client(create), [])
            runtime.request(client(create), [])
            self.assertEqual(budget.attempts, 2)
        self.assertEqual(runtime.stats['capability_retries'], 4)
        self.assertEqual(runtime.stats['successful_requests'], 2)

    def test_auth_failure_is_not_a_capability_or_transport_retry(self):
        runtime = self.make(reasoning_policy="lowest")
        for status in (401, 403):
            with self.assertRaisesRegex(RuntimeError, '鉴权失败'):
                runtime.request(client(lambda **_: (_ for _ in ()).throw(FakeError(status))), [])
        self.assertEqual(runtime.stats['requests'], 2)
        self.assertEqual(runtime.stats['capability_retries'], 0)
        self.assertFalse(parameter_rejection(FakeError(400, 'bad request content'), {'reasoning_effort':'none'}))
        self.assertTrue(parameter_rejection(FakeError(400, 'unsupported reasoning_effort value none'),
            {'reasoning_effort':'none','extra_body':{'thinking':{'type':'disabled'}}}))

    def test_negotiation_cached_without_credentials_or_content(self):
        with tempfile.TemporaryDirectory() as temp:
            options = {"concurrency_mode": "manual", "request_rate": 64, "reasoning_policy": "lowest", "profile_id": "one"}
            path = Path(temp)/'cap.json'
            stats = Path(temp)/'stats.json'
            runtime = RequestRuntime(options, capability_path=path, stats_path=stats, model='custom', base_url='https://one.test')
            seen = []
            def call(**kwargs):
                seen.append(kwargs.get('reasoning_effort'))
                if kwargs.get('reasoning_effort') != 'low':
                    raise FakeError(400, 'Unsupported value for reasoning_effort. Supported values: low')
                return response()
            runtime.request(client(call), [{'role':'user', 'content':'private document do-not-log'}])
            self.assertEqual(seen, ['none', 'minimal', 'low'])
            again = RequestRuntime(options, capability_path=path, model='custom', base_url='https://one.test')
            self.assertEqual(again.selected, {'reasoning_effort':'low'})
            different = RequestRuntime({**options,'profile_id':'two'}, capability_path=path, model='custom', base_url='https://one.test')
            self.assertIsNone(different.selected)
            self.assertNotIn('private document', path.read_text()+stats.read_text())
            self.assertEqual(runtime.stats['usage']['reasoning_tokens'], 0)
            self.assertFalse(runtime.stats['reasoning_confirmed'])

    def test_both_pools_share_concurrency_and_cancellation(self):
        runtime = self.make(max_in_flight=2)
        active = peak = 0
        lock = threading.Lock()
        def call(**kwargs):
            nonlocal active, peak
            with lock:
                active += 1; peak = max(peak, active)
            time.sleep(.035)
            with lock:
                active -= 1
            return response()
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda _: runtime.request(client(call), []), range(12)))
        self.assertLessEqual(peak, 2)
        self.assertEqual(runtime.stats['successful_requests'], 12)
        runtime.cancel_check = lambda: (_ for _ in ()).throw(RuntimeError('cancelled'))
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            runtime.request(client(call), [])

    def test_429_halves_limits_and_honors_retry_after(self):
        runtime = self.make(max_in_flight=8, request_rate=8)
        pauses = []
        def pause(delay, budget):
            pauses.append(delay)
            runtime.cooldown_until = 0
        runtime.pause = pause
        attempts = 0
        def call(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1: raise FakeError(429, retry_after='9')
            return response()
        runtime.request(client(call), [])
        self.assertEqual(runtime.limit, 4)
        self.assertEqual(runtime.rate, 4)
        self.assertGreaterEqual(pauses[0], 9)

    def test_auto_ramp_and_actual_minimum_policies(self):
        self.assertEqual(normalize_options({'concurrency_mode':'auto','max_in_flight':64})['max_in_flight'],16)
        self.assertEqual(normalize_options({'concurrency_mode':'manual','max_in_flight':64})['max_in_flight'],64)
        runtime = self.make(concurrency_mode='auto', max_in_flight=16)
        for _ in range(8): runtime.request(client(lambda **_: response()), [])
        self.assertEqual(runtime.limit, 6)
        self.assertEqual(capability_candidates('gpt-6.1-sol')[0], {'reasoning_effort':'low'})
        self.assertEqual(capability_candidates('deepseek-flash')[0]['extra_body']['thinking']['type'], 'disabled')
        self.assertEqual(capability_candidates('deepseek-flash', {'effort':{'supported_levels':['low','high','max']}})[0],
            {'reasoning_effort':'none','extra_body':{'thinking':{'type':'disabled'}}})
        budget = RequestBudget(deadline=time.monotonic() - 1)
        with self.assertRaises(RuntimeError): budget.remaining()

    def test_nonfinite_rate_rejected_and_timeout_retry_is_bounded(self):
        for value in ('nan','inf','-inf'):
            with self.assertRaises(ValueError): normalize_options({'request_rate':value})
        runtime = self.make()
        runtime.pause = lambda *_: None
        timeout = type('APITimeoutError', (Exception,), {})
        with self.assertRaisesRegex(RuntimeError, '5 次'):
            runtime.request(client(lambda **_: (_ for _ in ()).throw(timeout())), [])
        self.assertEqual(runtime.stats['upstream_errors'], 5)


if __name__ == '__main__': unittest.main()
