"""Owner synchronization retains binding identity across asynchronous work."""

from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.js_runtime


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is unavailable")
def test_dashboard_owner_sync_survives_rebinding_and_current_failure():
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("frontend_owner_sync_runtime.mjs"))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
