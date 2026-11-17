"""Real LVGL9.5 pointer pipeline, hit testing, release and bounded ownership."""
import os
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = ("hit", "s3", "bounds_busy", "cancel", "rotation", "allocation", "swipe", "swipe_scroll", "swipe_cancel")
pytestmark = [pytest.mark.diagnostic_reference, pytest.mark.integration, pytest.mark.timeout(180)]


@pytest.fixture(scope="module")
def touch_binary(tmp_path_factory):
    source = Path(os.environ["LVGL_HOST_SOURCE"]).resolve()
    assert (source / ".component_hash").read_text().strip() == "184e532558c1c45fefed631f3e235423d22582aafb4630f3e8885c35281a49ae"
    work = tmp_path_factory.mktemp("lvgl-touch")
    commands = [
        ["cmake", "-S", str(ROOT / "tests/fixtures/lvgl_touch"), "-B", str(work),
         f"-DLVGL_SOURCE={source}", "-DCMAKE_BUILD_TYPE=Release"],
        ["cmake", "--build", str(work), "--target", "touch_oracle", "--parallel", "2"],
    ]
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, timeout=150)
        assert result.returncode == 0, result.stdout + result.stderr
    return work / "touch_oracle"


@pytest.mark.parametrize("case", CASES)
def test_real_lvgl_coordinate_tap(touch_binary, case):
    result = subprocess.run([str(touch_binary), case], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith(f"PASS real_lvgl_9_5 {case}")
