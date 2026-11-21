"""Runs the editor's pure-model tests with node, when node is installed."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_design_model_in_node():
    r = subprocess.run(["node", "--test", str(Path(__file__).parent / "js" / "model.test.mjs")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-500:]
