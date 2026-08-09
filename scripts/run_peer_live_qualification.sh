#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

ha_python=${HA_PYTHON:?HA_PYTHON must select the Home Assistant test environment}
evidence_root=${1:-evidence/results}
policy_endpoint_id=${2:-${VOIP_QUALIFICATION_POLICY_ENDPOINT_ID:-}}
[[ -n "$policy_endpoint_id" ]] || {
  echo "peer-live requires an explicit policy endpoint as argument 2 or VOIP_QUALIFICATION_POLICY_ENDPOINT_ID" >&2
  exit 2
}
mkdir -p "$evidence_root"

"$ha_python" scripts/run_ha_real_matrix.py \
  --out-dir "$evidence_root/ha-real-matrix" \
  --policy-endpoint-id "$policy_endpoint_id" \
  --summary-output "$evidence_root/peer-live.json"
"$ha_python" scripts/run_dtmf_precedence_lab.py \
  --out "$evidence_root/dtmf-extension-precedence.json"
"$ha_python" scripts/run_sip_extensions_lab.py \
  --out-dir "$evidence_root/sip-extensions"
"$ha_python" scripts/run_sipp_rfc_lab.py \
  --out-dir "$evidence_root/sipp-rfc"

certificate_dir=$(mktemp -d /tmp/voip-peer-tls-XXXXXX)
trap 'rm -rf -- "$certificate_dir"' EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj /CN=localhost \
  -addext subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1 \
  -addext extendedKeyUsage=serverAuth \
  -keyout "$certificate_dir/server.key" \
  -out "$certificate_dir/server.crt" >/dev/null 2>&1
SSL_CERT_FILE="$certificate_dir/server.crt" "$ha_python" \
  scripts/run_sip_transport_lab.py \
  --ca-cert "$certificate_dir/server.crt" \
  --server-cert "$certificate_dir/server.crt" \
  --server-key "$certificate_dir/server.key" \
  --out-dir "$evidence_root/sip-transport"
