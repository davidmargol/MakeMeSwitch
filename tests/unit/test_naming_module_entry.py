"""Exercise -m/runpy entry, not only imported helpers; no host network changes."""
import os
import subprocess
import sys
from pathlib import Path


def test_module_entry_reuses_canonical_lock(tmp_path):
    code = '''
import runpy, sys
from makemeswitch import nic_names
from makemeswitch.core import engine
class FakeEngine:
    def refresh(self):
        with nic_names.naming_lock():
            raise RuntimeError("nested lock reached")
engine.Engine = FakeEngine
sys.argv = ["makemeswitch.nic_names", "apply"]
runpy.run_module("makemeswitch.nic_names", run_name="__main__")
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[2], env=dict(os.environ, MMS_NAMING_LOCK=str(tmp_path/'lock')), capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert 'nested lock reached' in result.stderr
