import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'darwin', 'macOS native profile store')
class NativeProfilesTests(unittest.TestCase):
    def test_native_history_and_key_isolation_with_memory_stores(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder)/'profiles-test'
            subprocess.run(['xcrun', 'swiftc', '-module-cache-path', str(ROOT/'.runtime/swift-cache'),
                str(ROOT/'desktop/ProviderStore.swift'), str(ROOT/'tests/ProviderStoreTests.swift'),
                '-o', str(binary)], check=True, cwd=ROOT, capture_output=True, text=True)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertIn('redaction PASS', result.stdout)
