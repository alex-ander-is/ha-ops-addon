"""Portable frontend behavior guards; rendered proof is a separate browser flow.

The Node helper evaluates the complete production module with inert Lit/Vaadin
imports. It does not claim DOM retention, native focus or visual verification.
HA_OPS_FRONTEND_SOURCE permits a disposable historical source for red/green proof.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


HELPER = Path(__file__).parent / "browser" / "preview-decisions-source.cjs"


@pytest.mark.parametrize("scenario", [
    "local", "lifetime", "final", "backup", "fences", "native", "lazyDiff", "transport",
])
def test_complete_frontend_preview_decision_contract(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node frontend runtime required for complete-module behavior regressions")
    result = subprocess.run(
        [node, str(HELPER), scenario], check=False, capture_output=True, text=True,
        timeout=30, env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["completeProductionModule"] is True
    assert evidence["rendered"] is False
    assert evidence["checks"] > 0
