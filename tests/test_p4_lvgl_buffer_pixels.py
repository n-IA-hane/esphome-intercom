"""Compare complete production P4 pixels through real LVGL tiled rotation."""
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import sys

from esphome import yaml_util
from PIL import Image, ImageChops
import pytest

from tests.test_lvgl_settings_composition import expanded_scene


ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.architecture, pytest.mark.slow, pytest.mark.timeout(600)]


@pytest.mark.parametrize("scene,width,height", [("p4-landscape", 1280, 800), ("p4-portrait", 800, 1280)])
def test_p4_buffer_quarter_matches_full_pixels_through_rotated_flush(tmp_path, scene, width, height):
    source = tmp_path / "expanded.yaml"
    source.write_text(yaml_util.dump(expanded_scene(scene, "full")))
    (tmp_path / "secrets.yaml").write_text("wifi_ssid: fixture-network\nwifi_password: fixture-password\n")
    build = Path(os.environ.get("LVGL_HOST_BUILD_ROOT", "/var/tmp/voip-builds/p4-buffer-pixels")) / scene
    report = {"scene": scene, "logical_dimensions": [width, height], "variants": [],
              "scope": "Actual production widgets, ESPHome software rotation and LVGL9.5 draw buffers; no physical panel, DMA, PPA or audio proof"}
    baseline = {}
    turns = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
    for rotation in (0, 90, 180, 270):
        images = {}
        for percent in (100, 25):
            output = tmp_path / f"rotation-{rotation}-buffer-{percent}"
            command = [sys.executable, str(ROOT / "tools/render_lvgl.py"), "--input", str(source),
                       "--page", "settings_p4_page", "--width", str(width), "--height", str(height),
                       "--output", str(output), "--build-parent", str(build),
                       "--buffer-percent", str(percent), "--rotation", str(rotation)]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=480)
            logs = "\n".join(p.read_text(errors="replace")[-4000:] for p in
                             (output / "compile.log", output / "run.log") if p.exists())
            assert result.returncode == 0, result.stdout + result.stderr + logs
            log = (output / "run.log").read_text()
            match = re.search(r"UI_BUFFER bytes=(\d+) logical_width=(\d+) logical_height=(\d+)", log)
            assert match, log[-3000:]
            assert tuple(map(int, match.groups()[1:])) == (width, height)
            assert int(match[1]) == width * height * 2 * percent // 100
            images[percent] = {name: Image.open(output / f"{name}.png").convert("RGB")
                               for name in ("top", "mid", "end")}
            report["variants"].append({"rotation": rotation, "buffer_percent": percent,
                "draw_buffer_bytes": int(match[1]), "pixel_sha256": {
                    name: hashlib.sha256(picture.tobytes()).hexdigest() for name, picture in images[percent].items()}})
            (tmp_path / "pixel-comparison.json").write_text(json.dumps(report, indent=2) + "\n")
        for name in ("top", "mid", "end"):
            full, quarter = images[100][name], images[25][name]
            difference = ImageChops.difference(full, quarter)
            if difference.getbbox() is not None:
                difference.save(tmp_path / f"difference-{rotation}-{name}.png")
            assert full.size == quarter.size and full.tobytes() == quarter.tobytes(), (scene, rotation, name, difference.getbbox())
            if rotation == 0:
                baseline[name] = full
            else:
                expected = baseline[name].transpose(turns[rotation])
                assert full.size == expected.size and full.tobytes() == expected.tobytes(), (scene, rotation, name, "rotation differs from the independent image transpose")
    report["all_pixels_equal"] = True
    report["rotation_pixels_equal"] = True
    (tmp_path / "pixel-comparison.json").write_text(json.dumps(report, indent=2) + "\n")
