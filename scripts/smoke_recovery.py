"""Exercise failure recovery through a packaged engine with a local fake API.

Protocol/output checks only; this provider cannot assess translation quality.
"""
import argparse
import collections
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import fitz
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tests'))
from mock_provider import ProviderHandler
from http.server import ThreadingHTTPServer


class FaultProvider(ProviderHandler):
    counts = collections.Counter()
    lock = threading.Lock()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        mode = body['model']
        with self.lock:
            self.counts[mode] += 1
            count = self.counts[mode]
        if mode == 'flaky' and count <= 2:
            return self.respond({'error':{'message':'injected upstream error'}}, 429 if count == 1 else 503)
        content = body['messages'][-1]['content']
        batched = '## Here is the input:' in content
        entries = json.loads(content.rsplit('## Here is the input:',1)[1].strip()) if batched else []

        def translate(text):
            markers = re.findall(r"\{[^{}]*\}|</?style[^>]*>",text)
            return ('这是模拟接口返回的研究段落译文。' + ('' if mode == 'markers' else ''.join(markers)))

        answer = translate(content.rsplit('Input:\n\n',1)[-1])
        if batched:
            selected = entries[:1] if mode == 'partial' else entries
            answer = json.dumps([{'id':e['id'],'output':translate(e['input'])} for e in selected],ensure_ascii=False)
            if mode == 'plain' or (mode == 'split' and len(entries)>1):
                answer = 'malformed JSON'
        if mode == 'invalid':
            answer = '[]'
        choices = [{'index':0,'message':{'role':'assistant','content':answer},
            'finish_reason':'length' if mode == 'length' and len(entries)>1 else 'stop'}]
        if mode == 'empty' and count <= 3:
            choices = []
        return self.respond({'id':'injected-test','object':'chat.completion','created':0,'model':mode,
            'choices':choices,'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--formula-fixture',type=Path,
        help='Optional PDF with inline formulas/styles for the protected-marker case')
    args = parser.parse_args()
    root = args.output.resolve().parent
    root.mkdir(parents=True,exist_ok=True)
    support = root/'support'
    support.mkdir(exist_ok=True)
    if not (support/'engine-cache').exists():
        (support/'engine-cache').symlink_to(ROOT/'backend/data/engine-cache',target_is_directory=True)
    fixture = root/'fixture.pdf'
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((60,60),'Language research',fontsize=18)
        for i in range(8):
            page.insert_textbox(fitz.Rect(60,100+i*75,535,160+i*75),
                f'Research paragraph {i}. Language experience influences learning. '
                'The researchers compare the conditions and report the results.',fontsize=11)
        page.insert_text((60,735),'The result is ',fontname='helv',fontsize=11)
        page.insert_text((125,735),'significant',fontname='heit',fontsize=11)
        page.insert_text((180,735),' in all conditions.',fontname='helv',fontsize=11)
        pdf.save(fixture)
    provider = ThreadingHTTPServer(('127.0.0.1',0),FaultProvider)
    threading.Thread(target=provider.serve_forever,daemon=True).start()
    state = support/'service.json'
    state.unlink(missing_ok=True)
    token = 'local-recovery-validation'
    env = dict(os.environ, PDF_STUDIO_SUPPORT_DIR=str(support),PDF_STUDIO_STATE_FILE=str(state),
        PDF_STUDIO_SESSION_TOKEN=token,PDF_STUDIO_WEB_DIR=str(ROOT/'build/desktop-web'),
        PDF_STUDIO_PARENT_PID=str(os.getpid()))
    results = []
    with (root/'engine.log').open('w') as log:
        process = subprocess.Popen([str(args.engine.resolve())],env=env,stdout=log,stderr=log)
        try:
            for _ in range(240):
                if state.exists(): break
                assert process.poll() is None,'engine exited'
                time.sleep(.25)
            base = 'http://127.0.0.1:'+str(json.loads(state.read_text())['port'])
            url = f'http://127.0.0.1:{provider.server_port}/v1'
            with httpx.Client(base_url=base,headers={'Authorization':'Bearer '+token},timeout=60,trust_env=False) as client:
                for _ in range(120):
                    try:
                        if client.get('/api/health').is_success:break
                    except httpx.ConnectError:pass
                    time.sleep(.25)
                for mode in ['split','partial','plain','markers','length','empty','flaky','invalid']:
                    chosen = fixture
                    if mode == 'markers' and args.formula_fixture:
                        chosen = root/'formula-fixture.pdf'
                        with fitz.open(args.formula_fixture) as source, fitz.open() as pdf:
                            for page in [1,5,12]:
                                if page < len(source):
                                    pdf.insert_pdf(source,from_page=page,to_page=page)
                            pdf.save(chosen)
                    with fitz.open(chosen) as source:
                        expected_pages = len(source)
                    client.post('/api/desktop/profile',json={'base_url':url,'api_key':'local-test-key','model':mode,
                        'runtime':{'concurrency_mode':'manual','max_in_flight':2,'request_rate':64,
                                   'reasoning_policy':'provider_default'}}).raise_for_status()
                    with chosen.open('rb') as source:
                        response = client.post('/api/jobs',files={'file':('fixture.pdf',source,'application/pdf')},
                            data={'model':mode,'use_cache':'false'})
                    response.raise_for_status()
                    id_ = response.json()['id']
                    deadline = time.monotonic()+180
                    while True:
                        job = client.get('/api/jobs/'+id_).json()
                        if job['status'] not in {'queued','running','cancelling'}:break
                        assert time.monotonic()<deadline,'bounded case exceeded 180 seconds'
                        time.sleep(.2)
                    if mode == 'invalid':
                        assert job['status']=='failed',job
                        assert '5 次' in job['error_message'],job
                        assert client.get(f'/api/jobs/{id_}/download').status_code==404
                        assert FaultProvider.counts[mode]<50,FaultProvider.counts[mode]
                    else:
                        assert job['status']=='completed',job
                        assert job['dual_requested'] is True
                        assert job['artifacts'][0]['kind']=='dual'
                        stats = job['runtime_stats']
                        if mode in {'split','partial','plain','length','empty'}:
                            assert stats['protocol_retries']>0,stats
                        if mode == 'plain':
                            assert stats['single_fallbacks']>0,stats
                        if mode == 'markers' and args.formula_fixture:
                            assert stats['protected_fallbacks']>0,stats
                        if mode == 'flaky':
                            assert stats['rate_limits']>0 and stats['upstream_errors']>0,stats
                        for route in ['download','download-dual']:
                            response = client.get(f'/api/jobs/{id_}/{route}')
                            response.raise_for_status()
                            with fitz.open(stream=response.content,filetype='pdf') as pdf:
                                assert len(pdf)==expected_pages
                                assert '研究' in pdf[0].get_text()
                        for route in ['source.txt','translated.txt']:
                            response=client.get(f'/api/jobs/{id_}/{route}')
                            response.raise_for_status()
                            assert response.content
                    results.append({'case':mode,'status':job['status'],'runtime':job['runtime_stats'],
                        'elapsed_seconds':job['elapsed_seconds'],'error':job.get('error_message')})
                    args.output.write_text(json.dumps({'cases':results,'scope':'local protocol and delivery validation'},ensure_ascii=False,indent=2))
                    print(f'{mode}: PASS ({job["status"]}, {FaultProvider.counts[mode]} HTTP calls)',flush=True)
        finally:
            provider.shutdown()
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL)
                process.wait()


if __name__=='__main__':
    main()
