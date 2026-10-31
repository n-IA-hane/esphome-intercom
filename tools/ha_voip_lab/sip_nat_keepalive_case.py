"""Production native REGISTER stays reachable beyond an actual NAT idle timer.

The maintained C++ probe only replaces physical I/O and answers ordinary
OPTIONS. This qualifies registration/signaling, not native call media or ESP.
"""

import asyncio
import os
from pathlib import Path
import secrets
import subprocess
from unittest.mock import AsyncMock

import pytest
import pytest_socket

from tests.test_automation_originate_ha import lab as lab
from custom_components.voip_stack import endpoint_runtime, peer_snapshot
from custom_components.voip_stack.const import CONF_REGISTRAR_ENABLED
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.runtime_data import sip_endpoint_manager
from custom_components.voip_stack.sip_registrar import SipAccount
from tools.ha_voip_lab.sip_nat_lab import CLIENT_IP, NAT_IP, SERVER_IP, private_json


pytestmark = pytest.mark.ha


@pytest.mark.parametrize("transport", ["udp", "tcp"])
async def test_native_registration_remains_reachable_beyond_nat_idle(lab, monkeypatch, transport):
    namespace, probe = os.environ["SIP_NAT_CLIENT_NS"], Path(os.environ["SIP_NAT_NATIVE_PROBE"])
    interval = int(os.environ["SIP_NAT_NATIVE_KEEPALIVE_INTERVAL"])
    rebind = os.environ.get("SIP_NAT_REBIND") == "1"
    output = Path(os.environ["SIP_NAT_OUTPUT"]) / transport
    output.mkdir(parents=True, mode=0o700)
    pytest_socket.socket_allow_hosts(["127.0.0.1", CLIENT_IP, NAT_IP, SERVER_IP])
    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    lab.runtime.transport_config.update({"sip_port": 15060, "rtp_port": 18000, "advertise_host": SERVER_IP,
        CONF_REGISTRAR_ENABLED: True})
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value=SERVER_IP))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    registrar = lab.calls.component("registrar")
    username, password = "ordinary_native_idle", secrets.token_hex(24)
    registrar.update_accounts([SipAccount(username, username, password)])
    manager = sip_endpoint_manager(lab.hass)
    log_path = output / "native-probe.log"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as log:
        process = subprocess.Popen(["ip", "netns", "exec", namespace, str(probe), transport,
            SERVER_IP, "15060", username, SERVER_IP, "15"], stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ, "REG_TEST_PASSWORD": password, "REG_TEST_EXPIRES": "3600",
                "REG_TEST_KEEPALIVE_SECONDS": str(interval)})
    result = {"passed": False, "transport": transport, "registration_expires": 3600,
        "nat_udp_idle_timeout": 5, "native_probe_pid": process.pid, "physical_esp_qualified": False}
    result["keepalive_interval_seconds"] = interval
    try:
        async with asyncio.timeout(5):
            while not registrar.registered_contacts(username):
                assert process.poll() is None
                await asyncio.sleep(.05)
        binding, = registrar.registered_contacts(username)
        result["initial_registration_flow"] = binding.snapshot()
        assert binding.source_host == NAT_IP and CLIENT_IP in binding.advertised_contact_uri
        initial = await manager.async_options(binding.contact_uri, (binding.source_host, binding.source_port), transport.upper(), timeout=2)
        assert initial is not None and initial.status_code == 200
        result["initial_request_status"] = initial.status_code
        # No calls, traffic generators or server probes keep this flow alive.
        await asyncio.sleep(8)
        assert registrar.registered_contacts(username)
        current, = registrar.registered_contacts(username)
        result["after_idle_registration_flow"] = current.snapshot()
        if rebind and transport == "udp":
            assert current.source_port != binding.source_port and current.cseq > binding.cseq
            assert current.advertised_contact_uri == binding.advertised_contact_uri
            assert current.call_id == binding.call_id
        response = await manager.async_options(current.contact_uri, (current.source_host, current.source_port), transport.upper(), timeout=2)
        result["after_idle_request_status"] = response.status_code if response is not None else None
        if interval == 0 and transport == "udp":
            assert response is None  # Disabled retains the independently reproduced baseline.
        else:
            assert response is not None and response.status_code == 200
        result["passed"] = True
    finally:
        try:
            await asyncio.to_thread(process.wait, timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=5)
        result["native_probe_exit_code"] = process.returncode
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
        private_json(output / "result.json", result)
        pytest_socket.socket_allow_hosts(["127.0.0.1"])
