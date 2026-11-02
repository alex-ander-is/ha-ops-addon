"""Portable real-method deferred response proof; rendered checks stay separate."""
from pathlib import Path
import shutil
import subprocess
import pytest


def test_migration_review_lifecycle_fences_deferred_responses():
    node=shutil.which('node')
    if node is None and Path('/opt/homebrew/bin/node').is_file():node='/opt/homebrew/bin/node'
    if node is None:pytest.skip('Node is required for supplemental frontend method proof')
    script=Path(__file__).parent/'browser/migration-review-lifecycle.mjs'
    result=subprocess.run([node,str(script)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    assert '"status":"passed"' in result.stdout
