import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

class SpawnCacheTests(unittest.TestCase):
    def test_direct_worker_import_uses_inherited_cache_without_app_import(self):
        with tempfile.TemporaryDirectory() as temp:
            canonical=Path(temp)/'canonical'
            fresh=Path(temp)/'fresh-user'
            fresh.mkdir()
            env=dict(os.environ,PDF_TRANSLATION_CACHE_DIR=str(canonical),
                XDG_CACHE_HOME=str(Path(temp)/'xdg-cache'),XDG_CONFIG_HOME=str(Path(temp)/'xdg-config'))
            code='''import json,os,sys
from pathlib import Path
Path.home=classmethod(lambda cls: Path(sys.argv[2]))
sys.path.insert(0,sys.argv[1])
# A macOS-spawned child imports the target module before app.py.
import translation_runtime
import babeldoc.const as c
import babeldoc.assets.assets as assets
root=Path(os.environ['PDF_TRANSLATION_CACHE_DIR'])
assert c.CACHE_FOLDER==root
assert c.TIKTOKEN_CACHE_FOLDER==root/'tiktoken'
assert Path(os.environ['TIKTOKEN_CACHE_DIR'])==root/'tiktoken'
assert assets.get_cache_file_path('fixture.onnx','models')==root/'models/fixture.onnx'
assert assets.get_cache_file_path('fixture.ttf','fonts')==root/'fonts/fixture.ttf'
assert 'app' not in sys.modules
print(json.dumps({'worker_cache':str(c.CACHE_FOLDER),'legacy_assets_used':False}))
'''
            result=subprocess.run([sys.executable,'-c',code,str(ROOT/'backend'),str(fresh)],env=env,
                capture_output=True,text=True,timeout=45)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertFalse(json.loads(result.stdout)['legacy_assets_used'])
            self.assertFalse((fresh/'.cache/babeldoc/fonts').exists())
            self.assertFalse((fresh/'.cache/babeldoc/models').exists())

if __name__=='__main__':unittest.main()
