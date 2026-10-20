"""Two isolated loopback providers; credentials are disposable test fixtures."""
import json
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer
from mock_provider import ProviderHandler

root=Path(__file__).resolve().parents[1]/'.runtime/qa-0.4'
root.mkdir(parents=True,exist_ok=True)
seen=[]
servers=[]
for name in ('a','b'):
    def handler(name):
        class ProfileProvider(ProviderHandler):
            def do_GET(self):
                valid=self.headers.get('Authorization')==f'Bearer qa-key-{name}'
                seen.append({'provider':name,'route':'models','correct_key':valid})
                if not valid:return self.respond({'error':{'message':'wrong fixture credential'}},401)
                return self.respond({'data':[{'id':f'qa-model-{name}','context_window':32768,'max_output_tokens':4096,'effort':{'supported_levels':['none','low']}}]})
            def do_POST(self):
                valid=self.headers.get('Authorization')==f'Bearer qa-key-{name}'
                seen.append({'provider':name,'route':'completion','correct_key':valid})
                if not valid:return self.respond({'error':{'message':'wrong fixture credential'}},401)
                return super().do_POST()
        return ProfileProvider
    server=ThreadingHTTPServer(('127.0.0.1',0),handler(name))
    servers.append(server)
    threading.Thread(target=server.serve_forever,daemon=True).start()
configuration={name:f'http://127.0.0.1:{server.server_port}/v1' for name,server in zip(('a','b'),servers)}
(root/'providers.json').write_text(json.dumps(configuration))
print(json.dumps(configuration),flush=True)
try:
    threading.Event().wait()
finally:
    (root/'requests.json').write_text(json.dumps(seen))
