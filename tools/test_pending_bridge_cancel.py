"""SIPp regression: pending bridge cancellation must release every RTP port.

Run against an idle local HA lab with SIP on 15060. Set HA_BASE and
HA_LAB_CREDENTIALS_FILE for its existing private login. Pass a new output
directory, optionally followed by 'answered' to cover normal handoff/teardown.
Creates and removes a temporary qa_pending contact pointing to a local SIPp UAS.
"""

import asyncio, json, sys, subprocess, time, os
import aiohttp
from ha_voip_lab.auth import lab_token
from pathlib import Path

ROOT = Path(__file__).parent / "qualification" / "sipp"
HA_BASE = os.environ.get("HA_BASE", "http://127.0.0.1:18123").rstrip("/")
_token = None


async def request(_instance, command):
    global _token
    if _token is None:
        _token = await asyncio.to_thread(
            lab_token, HA_BASE, Path(os.environ["HA_LAB_CREDENTIALS_FILE"])
        )
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(HA_BASE + "/api/websocket") as ws:
            assert (await ws.receive_json())["type"] == "auth_required"
            await ws.send_json({"type": "auth", "access_token": _token})
            assert (await ws.receive_json())["type"] == "auth_ok"
            await ws.send_json({"id": 1, **command})
            while True:
                result = await ws.receive_json()
                if result.get("id") == 1:
                    if not result.get("success"):
                        raise RuntimeError(
                            str(result.get("error", {}).get("code", "request_failed"))
                        )
                    return result.get("result")


async def service(name, data):
    return await request(
        "lab",
        {
            "type": "call_service",
            "domain": "voip_stack",
            "service": name,
            "service_data": data,
        },
    )


async def state():
    r = await request("lab", {"type": "voip_stack/ha_softphone_state"})

    def find(x):
        if isinstance(x, dict):
            if "runtime_resources" in x:
                return x["runtime_resources"]
            for v in x.values():
                y = find(v)
                if y:
                    return y
        return None

    return find(r)


async def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True)
    report = {"before": await state()}
    uas = None
    assert report["before"]["call_scoped_quiescent"], (
        "Use an idle dedicated HA laboratory"
    )
    answered = len(sys.argv) > 2 and sys.argv[2] == "answered"
    try:
        await service(
            "add_contact",
            {
                "id": "qa_pending",
                "name": "qa_pending",
                "sip_uri": "sip:qa_pending@127.0.0.1:15062",
            },
        )
        with (out / "uas.log").open("wb") as f:
            uas = subprocess.Popen(
                [
                    "sipp",
                    "-sf",
                    str(ROOT / ("answer-uas.xml" if answered else "pending-uas.xml")),
                    "-i",
                    "127.0.0.1",
                    "-p",
                    "15062",
                    "-m",
                    "1",
                    "-timeout",
                    "25",
                    "-nostdin",
                ],
                stdout=f,
                stderr=subprocess.STDOUT,
            )
        with (out / "uac.log").open("wb") as f:
            uac = await asyncio.create_subprocess_exec(
                "sipp",
                "127.0.0.1:15060",
                "-sf",
                str(ROOT / ("answer-uac.xml" if answered else "cancel-uac.xml")),
                "-i",
                "127.0.0.1",
                "-p",
                "15064",
                "-m",
                "1",
                "-timeout",
                "25",
                "-nostdin",
                stdout=f,
                stderr=asyncio.subprocess.STDOUT,
            )
            report["uac_exit"] = await uac.wait()
        report["uas_exit"] = await asyncio.to_thread(uas.wait, 10)
        # Capture after SIP completion and once canonical owners have disappeared.
        for _ in range(20):
            report["after"] = await state()
            if report["after"]["resource_counts"]["sessions"] == 0:
                break
            await asyncio.sleep(0.25)
        report["status"] = (
            "passed"
            if report["uac_exit"] == report["uas_exit"] == 0
            and report["after"]["resource_counts"]
            == report["before"]["resource_counts"]
            else "failed"
        )
    finally:
        if uas and uas.poll() is None:
            uas.terminate()
            await asyncio.to_thread(uas.wait, 5)
        await service("remove_contact", {"name": "qa_pending"})
        (out / "result.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    if report["status"] != "passed":
        raise SystemExit(1)


asyncio.run(main())
