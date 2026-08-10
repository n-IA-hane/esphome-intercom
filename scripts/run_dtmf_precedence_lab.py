#!/usr/bin/env python3
"""Qualify exact DTMF extension precedence against the isolated HA lab."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))

from inbound_routing_qualification import (  # noqa: E402
    BareSip,
    EventTrace,
    FlowSnapshot,
    HomeAssistantApi,
    trace_types,
    wait_for,
    wait_event_state,
)
from live_voip_qualification import candidate_revision  # noqa: E402
from run_answered_sipp_lab import lab_token, runtime_quiescence  # noqa: E402
from run_ha_real_matrix import _registered_local_trunk  # noqa: E402


def _caller_config(source: Path, destination: Path, *, port: int) -> tuple[Path, str]:
    shutil.copytree(source, destination)
    config = destination / "config"
    text = config.read_text(encoding="utf-8")
    match = re.search(r"(?m)^sip_listen\s+(\S+):(\d+)\s*$", text)
    if match is None:
        raise RuntimeError("bareSIP source config has no sip_listen address")
    listen_host = match.group(1)
    text = text[: match.start(2)] + str(port) + text[match.end(2) :]
    config.write_text(text, encoding="utf-8")
    (destination / "accounts").write_text(
        f'"DTMF caller" <sip:sipp@{listen_host};transport=udp>'
        ';audio_codecs=PCMA;ptime=20;dtmfmode=rtp;regint=0\n',
        encoding="utf-8",
    )
    return destination, listen_host


def _extension_entities(api: HomeAssistantApi) -> list[tuple[str, str]]:
    return sorted(
        (
            str(state["entity_id"]).removeprefix("text.").removesuffix("_extension"),
            str(state.get("state") or "").strip(),
        )
        for state in api.get("/api/states")
        if str(state.get("entity_id") or "").startswith("text.")
        and str(state.get("entity_id") or "").endswith("_extension")
        and str(state.get("state") or "").strip().isdigit()
    )


def _run_case(
    api: HomeAssistantApi,
    *,
    config: Path,
    sip_host: str,
    sip_port: int,
    extension: str,
    callee: str,
    scenario_id: str,
) -> dict[str, object]:
    started = time.monotonic()
    caller = BareSip(config, wait_registered=False)
    try:
        with EventTrace(api) as trace:
            caller.dial(f"sip:9999@{sip_host}:{sip_port}")
            caller.wait_for_dtmf_media(8)
            caller.digits(extension, interval=0.08)
            event = wait_event_state(
                api,
                "ringing",
                8,
                expected_remote_party=callee,
            )
            time.sleep(0.1)
        events = trace_types(trace, str(event.get("call_id") or ""))
        if "route_requested" in events:
            raise RuntimeError(f"exact extension entered automation window: {events}")
        return {
            "name": scenario_id,
            "status": "passed",
            "duration_s": round(time.monotonic() - started, 3),
            "extension": extension,
            "callee": callee,
            "events": events,
        }
    except Exception as error:
        raise RuntimeError(f"{error}; bareSIP={caller.read()[-2000:]}") from error
    finally:
        caller.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha-url", default="http://127.0.0.1:18123")
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path("/home/codex/ha-voip-lab/.credentials"),
    )
    parser.add_argument(
        "--caller-config",
        type=Path,
        default=Path("/home/codex/ha-voip-lab/baresip-source"),
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    token = lab_token(args.ha_url, args.credentials)
    api = HomeAssistantApi(base_url=args.ha_url, token=token)
    snapshot = FlowSnapshot.capture(api)
    extensions = wait_for(
        lambda: (items if len(items := _extension_entities(api)) >= 2 else None),
        15,
        "two configured DTMF extensions",
    )
    fake_trunk = {
        "trunk_transport": "udp",
        "trunk_server": "",
        "trunk_port": 19999,
        "trunk_domain": "",
        "trunk_username": "sipp",
        "trunk_auth_username": "sipp",
        "trunk_password": "qualification-only",
        "trunk_expires": 60,
        "trunk_outbound_proxy": "",
        "trunk_dtmf_enabled": True,
    }
    results: list[dict[str, object]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="voip-dtmf-lab-") as temp:
            config, sip_host = _caller_config(
                args.caller_config,
                Path(temp) / "baresip",
                port=19999,
            )
            fake_trunk["trunk_server"] = sip_host
            fake_trunk["trunk_domain"] = sip_host
            with _registered_local_trunk(
                Path(temp),
                19999,
                host=sip_host,
            ) as trunk_contact_target:
                snapshot.apply(
                    api,
                    mode="dtmf",
                    automation=True,
                    default_target="Casa",
                    timeout_seconds=5,
                    trunk_override=fake_trunk,
                )
                _, sip_port = trunk_contact_target()
            for index, (slug, extension) in enumerate(extensions[:2]):
                callee = slug.replace("_", " ").title()
                scenario_id = (
                    "dtmf_primary_extension_bypasses_automation"
                    if index == 0
                    else "dtmf_secondary_extension_bypasses_automation"
                )
                try:
                    results.append(
                        _run_case(
                            api,
                            config=config,
                            sip_host=sip_host,
                            sip_port=sip_port,
                            extension=extension,
                            callee=callee,
                            scenario_id=scenario_id,
                        )
                    )
                except Exception as error:  # noqa: BLE001, preserve both cases.
                    results.append(
                        {"name": scenario_id, "status": "failed", "error": str(error)}
                    )
                with suppress(Exception):
                    api.service("voip_stack", "hangup")
                asyncio.run(runtime_quiescence(args.ha_url, token))
    finally:
        snapshot.apply(api)

    artifact = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "candidate": candidate_revision(),
        "results": results,
        "quiescence": asyncio.run(runtime_quiescence(args.ha_url, token)),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps(artifact, indent=2))
    return 1 if any(result["status"] != "passed" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
