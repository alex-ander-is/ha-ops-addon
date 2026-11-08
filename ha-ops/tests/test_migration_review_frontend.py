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



def test_browser_interception_failure_reaches_owned_cleanup():
    node=shutil.which('node')
    if node is None and Path('/opt/homebrew/bin/node').is_file():node='/opt/homebrew/bin/node'
    if node is None:pytest.skip('Node is required for browser harness proof')
    script=Path(__file__).parent/'browser/migration-review-interception-test.mjs'
    result=subprocess.run([node,str(script)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'outer cleanup' in result.stdout
    runner=(Path(__file__).parent/'browser/migration-review.mjs').read_text()
    assert 'stateIntercept.guard' in runner and 'heldIntercept.guard' in runner
    assert 'route.fetch(' not in runner
