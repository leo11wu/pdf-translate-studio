"""Reproducible fixed-latency scheduling benchmark, with no external API/key.

Calls the pinned upstream and project process_page methods on the same four-page
IL fixture. It measures JSON request organization + scheduling only, not PDF
layout inference or linguistic quality. A fake provider returns one CJK output
per original ID and simulates a constant 1.2 second network/model latency.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
import app  # Set the local engine cache before upstream imports.
from request_runtime import RequestRuntime
from translation_runtime import StudioBatchTranslator
from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import ILTranslatorLLMOnly
from babeldoc.format.pdf.document_il.utils import paragraph_helper


def batches(optimized):
    translator = StudioBatchTranslator.__new__(StudioBatchTranslator)
    translator.runtime = RequestRuntime({'reasoning_policy':'provider_default'})
    translator.translation_config = NS(raise_if_cancelled=lambda:None, min_text_length=1)
    translator.mid = 0
    translator.shared_context_cross_split_part = NS(first_paragraph=None, recent_title_paragraph=None)
    translator.translation_config.shared_context_cross_split_part = translator.shared_context_cross_split_part
    translator.calc_token_count = lambda text:100
    captured = []
    executor = NS(submit=lambda fn, batch, *args, **kwargs: captured.append(batch))
    tracker = NS(new_paragraph=lambda:NS())
    method = StudioBatchTranslator.process_page if optimized else ILTranslatorLLMOnly.process_page
    for page_index in range(4):
        paras = []
        for index in range(24):
            # A same-style text run is a real IL composition accepted by the
            # upstream filtering helpers. Token count is fixed at 100 above.
            text = f'Research paragraph {page_index}-{index}. ' + 'Language learning depends on experience. ' * 15
            composition = NS(pdf_line=None,pdf_same_style_characters=None,pdf_same_style_unicode_characters=NS(unicode=text),pdf_formula=None,pdf_character=None)
            para = NS(debug_id=f'{page_index}-{index}',unicode=text,layout_label='text',pdf_paragraph_composition=[composition])
            assert not paragraph_helper.is_placeholder_only_paragraph(para)
            paras.append(para)
        page = NS(pdf_font=[],pdf_xobject=[],pdf_paragraph=paras)
        method(translator,page,executor,tracker=tracker,translated_ids=set())
    coverage = [p.debug_id for batch in captured for p in batch.paragraphs]
    assert len(coverage) == len(set(coverage)) == 96
    return [[{'id':i,'input':p.unicode,'layout_label':'text'} for i,p in enumerate(batch.paragraphs)] for batch in captured]


def run(variant, latency):
    entries = batches(variant != 'original')
    runtime = RequestRuntime({'concurrency_mode':'auto' if variant == 'full' else 'manual',
        'max_in_flight':16 if variant == 'full' else 4, 'request_rate':2 if variant == 'full' else 4,
        'reasoning_policy':'lowest' if variant == 'full' else 'provider_default'}, model='fixture')
    batch = StudioBatchTranslator.__new__(StudioBatchTranslator)
    batch.runtime = runtime
    batch._build_llm_prompt = lambda text,*_:text
    def create(**kwargs):
        time.sleep(latency)
        items = json.loads(kwargs['messages'][0]['content'])
        output = json.dumps([{'id':row['id'],'output':'这是固定延迟测试的对应段落译文。'} for row in reversed(items)])
        return NS(choices=[NS(message=NS(content=output),finish_reason='stop')],usage=None)
    client = NS(chat=NS(completions=NS(create=create)))
    batch.translate_engine = NS(llm_translate=lambda text,**_: runtime.request(client,[{'role':'user','content':text}]).choices[0].message.content)
    def resolve(items):
        with runtime.budget():
            output = batch.resolve(items,None,None)
            assert set(output) == {row['id'] for row in items}
    start = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(resolve,entries))
    return {'variant':variant,'elapsed_seconds':round(time.monotonic()-start,3),
        'batch_sizes':[len(v) for v in entries],'paragraphs':96,**runtime.snapshot()}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'.runtime/benchmark-0.4/fixed-latency.json')
    parser.add_argument('--latency',type=float,default=1.2)
    parser.add_argument('--runs',type=int,default=2)
    args=parser.parse_args()
    results=[]
    for repetition in range(args.runs):
        for variant in ('original','batch_only','full'):
            row=run(variant,args.latency); row['repetition']=repetition+1
            results.append(row)
            print(f'{variant} #{repetition+1}: {row["requests"]} requests, {row["elapsed_seconds"]} seconds',flush=True)
    means={name:sum(r['elapsed_seconds'] for r in results if r['variant']==name)/args.runs for name in ('original','batch_only','full')}
    original=next(r for r in results if r['variant']=='original')
    full=next(r for r in results if r['variant']=='full')
    request_reduction=1-full['requests']/original['requests']
    time_reduction=1-means['full']/means['original']
    report={'scope':'same-page native IL batching + JSON validation + request scheduling; no PDF rendering or language quality claim',
        'fixture':{'pages':4,'paragraphs_per_page':24,'source_tokens_per_paragraph':100,'fixed_response_seconds':args.latency},
        'results':results,'mean_seconds':means,'request_reduction':request_reduction,'time_reduction':time_reduction,
        'acceptance_passed':request_reduction>=.5 and time_reduction>=.3}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    assert report['acceptance_passed'],report
    print(f'PASS: requests -{request_reduction:.1%}; elapsed -{time_reduction:.1%}',flush=True)

if __name__=='__main__':main()
