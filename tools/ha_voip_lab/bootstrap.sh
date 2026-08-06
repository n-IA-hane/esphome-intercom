#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
lab_root="${HA_VOIP_LAB_ROOT:-$HOME/ha-voip-lab}"
lab_user="${HA_VOIP_LAB_USER:-$(id -un)}"
lab_group="${HA_VOIP_LAB_GROUP:-$(id -gn)}"
ha_version="${HA_VOIP_LAB_VERSION:-2026.8.0}"
config_dir="$lab_root/config"

mkdir -p "$config_dir/custom_components"

if [[ ! -x "$lab_root/.venv/bin/hass" ]]; then
  uv venv --python 3.14 "$lab_root/.venv"
fi

installed_version=$(
  "$lab_root/.venv/bin/python" -c \
    'from importlib.metadata import version; print(version("homeassistant"))' \
    2>/dev/null || true
)
if [[ "$installed_version" != "$ha_version" ]]; then
  uv pip install --python "$lab_root/.venv/bin/python" \
    --upgrade "homeassistant==$ha_version"
fi

cp "$repo/tools/ha_voip_lab/configuration.yaml" "$config_dir/configuration.yaml"
ln -sfn "$repo/custom_components/voip_stack" "$config_dir/custom_components/voip_stack"

if command -v systemctl >/dev/null 2>&1; then
  service_file="$(mktemp)"
  trap 'rm -f "$service_file"' EXIT
  python3 - \
    "$repo/tools/ha_voip_lab/home-assistant-voip-lab.service.in" \
    "$service_file" "$lab_root" "$lab_user" "$lab_group" <<'PY'
from pathlib import Path
import sys

source, destination, root, user, group = sys.argv[1:]
rendered = (
    Path(source)
    .read_text()
    .replace("@LAB_ROOT@", root)
    .replace("@LAB_USER@", user)
    .replace("@LAB_GROUP@", group)
)
Path(destination).write_text(rendered)
PY
  sudo install -m 0644 \
    "$service_file" \
    /etc/systemd/system/home-assistant-voip-lab.service
  sudo systemctl daemon-reload
fi

printf 'VoIP Stack lab ready at %s\n' "$lab_root"
printf 'Start with: sudo systemctl start home-assistant-voip-lab.service\n'
