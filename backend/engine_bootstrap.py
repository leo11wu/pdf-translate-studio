"""Configure inherited resource paths before *any* PDF engine import.

macOS spawn and PyInstaller import the pickled worker module without running
app.py. Keep this lightweight hook ahead of pdf2zh_next/BabelDOC imports in
that module, so all constants copied by their asset modules see one root.
"""
from __future__ import annotations

import os
from pathlib import Path


def configure_engine_cache():
    fallback = (Path(os.environ['PDF_STUDIO_SUPPORT_DIR']) / 'engine-cache'
        if os.environ.get('PDF_STUDIO_SUPPORT_DIR') else Path(__file__).resolve().parent / 'data' / 'engine-cache')
    root = Path(os.environ.get('PDF_TRANSLATION_CACHE_DIR', str(fallback))).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ['PDF_TRANSLATION_CACHE_DIR'] = str(root)
    import babeldoc.const as constants
    constants.CACHE_FOLDER = root
    constants.TIKTOKEN_CACHE_FOLDER = root / 'tiktoken'
    constants.TIKTOKEN_CACHE_FOLDER.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ['TIKTOKEN_CACHE_DIR'] = str(constants.TIKTOKEN_CACHE_FOLDER)
    return root
