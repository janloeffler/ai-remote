import shutil
import subprocess
from pathlib import Path

import pytest

JS_DIR = Path(__file__).parent / "js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_node_e2e_core_interop():
    result = subprocess.run(
        ["node", "--test", *sorted(str(p) for p in JS_DIR.glob("*.test.mjs"))],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
