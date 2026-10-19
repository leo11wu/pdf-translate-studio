from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata
from pathlib import Path

root = Path(SPECPATH).parent

datas = collect_data_files('babeldoc') + collect_data_files('pdf2zh_next')
for distribution in ['pdf2zh-next', 'babeldoc', 'onnxruntime', 'onnx', 'tiktoken', 'pymupdf']:
    datas += copy_metadata(distribution)

a = Analysis(
    [str(root / 'desktop/server.py')], pathex=[str(root / 'backend')], datas=datas,
    hiddenimports=collect_submodules('bitstring') + [
        'pdf2zh_next.translator.translator_impl.openai',
        'tiktoken_ext.openai_public', 'uvicorn.logging',
        'uvicorn.protocols.http.h11_impl', 'uvicorn.lifespan.on',
        'babeldoc.format.pdf.document_il.il_version_1',
    ],
    excludes=[
        'gradio', 'gradio_client', 'gradio_pdf', 'gradio_i18n', 'pandas',
        'matplotlib', 'tkinter', 'IPython', 'pytest', 'notebook', 'jupyter',
        'torch', 'tensorflow', 'transformers', 'huggingface_hub',
        'azure', 'deepl', 'xinference_client', 'ollama', 'tencentcloud',
        'ruff', 'onnx.backend.test', 'onnx.reference', 'onnx.tools',
        'onnxruntime.tools', 'scipy.tests', 'sklearn.tests',
        'cmake', 'ninja', 'skbuild', 'wheel', 'uvloop', 'watchfiles', 'httptools',
    ],
    noarchive=False,
)
# Test fixtures and C/C++ development headers are never runtime assets.
a.datas = [entry for entry in a.datas if not any(part in entry[0].split('/')
           for part in ('tests', 'test', 'include', '__pycache__'))]
# The Python extension embeds ORT and links directly to CoreML. The standalone
# C API library is not linked by any shipped binary or used by this app.
a.binaries = [entry for entry in a.binaries if not (
    entry[0].startswith('onnxruntime/capi/libonnxruntime.') and entry[0].endswith('.dylib'))]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='studio-engine',
          debug=False, strip=False, upx=False, console=True, target_arch='arm64')
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='studio-engine')
app = BUNDLE(coll, name='StudioEngine.app', bundle_identifier='org.paperstudio.engine',
             info_plist={'LSBackgroundOnly': True, 'LSMinimumSystemVersion': '14.0'})
