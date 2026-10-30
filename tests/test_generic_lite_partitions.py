"""The 4 MB Lite layout retains two OTA slots and an explicit NVS budget."""
import csv
from pathlib import Path
import subprocess
import sys

from esphome import yaml_util

from tests.test_package_composition import device_config

ROOT = Path(__file__).resolve().parents[1]
PROFILE = 'yamls/full-experience/single-bus/generic-s3-full-lite-aec.yaml'
CSV = ROOT / Path(PROFILE).parent / 'partitions_4mb_lite_dual_ota.csv'


def test_lite_table_fits_four_megabytes_and_preserves_system_partitions():
    rows = {}
    for row in csv.reader(CSV.read_text().splitlines()):
        if not row or row[0].lstrip().startswith('#'):
            continue
        name, kind, subtype, offset, size = [value.strip() for value in row]
        rows[name] = (kind, subtype, int(offset, 0), int(size, 0))
    assert rows == {
        'otadata': ('data', 'ota', 0x9000, 0x2000),
        'phy_init': ('data', 'phy', 0xB000, 0x1000),
        'app0': ('app', 'ota_0', 0x10000, 0x1E0000),
        'app1': ('app', 'ota_1', 0x1F0000, 0x1E0000),
        'nvs': ('data', 'nvs', 0x3D0000, 0x30000),
    }
    ordered = sorted(rows.values(), key=lambda row: row[2])
    assert all(left[2] + left[3] <= right[2] for left, right in zip(ordered, ordered[1:]))
    assert ordered[-1][2] + ordered[-1][3] == 4 * 1024 * 1024
    assert all(row[2] % 0x10000 == 0 for row in rows.values() if row[0] == 'app')
    assert rows['nvs'][3] == 16 * 0x3000
    users = [path.name for path in (ROOT / 'packages/board').glob('*.yaml') if CSV.name in path.read_text()]
    assert users == ['generic-s3-lite-aec.yaml']


def test_lite_companion_csv_resolves_beside_renamed_main_and_reaches_codegen(tmp_path):
    original = yaml_util.load_yaml(ROOT / PROFILE)
    config = device_config(tmp_path, original['substitutions']['firmware_features'], PROFILE)
    assert config.name == 'device.yaml'  # The user may rename the entrypoint, while CSV keeps its published name.
    assert (tmp_path / CSV.name).read_bytes() == CSV.read_bytes()
    result = subprocess.run([sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)], capture_output=True, text=True, timeout=150)
    assert result.returncode == 0, result.stdout + result.stderr
    generated = next(tmp_path.rglob('partitions.csv'))
    assert generated.read_bytes() == CSV.read_bytes()
    defines = next(tmp_path.rglob('defines.h')).read_text()
    assert 'USE_RUNTIME_CONTROLLER' in defines
    assert 'USE_MICRO_WAKE_WORD' in defines and 'USE_VOICE_ASSISTANT' in defines
    assert 'USE_SENDSPIN' not in defines
