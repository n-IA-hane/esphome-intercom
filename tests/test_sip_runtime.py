"""Behavioral tests for shared SIP runtime helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load_module(name: str):
    if "homeassistant" not in sys.modules:
        package = types.ModuleType("homeassistant")
        package.__path__ = []
        sys.modules["homeassistant"] = package
    if "homeassistant.core" not in sys.modules:
        core = types.ModuleType("homeassistant.core")
        core.HomeAssistant = type("HomeAssistant", (), {})
        sys.modules["homeassistant.core"] = core
    if "custom_components" not in sys.modules:
        root_package = types.ModuleType("custom_components")
        root_package.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root_package
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package
    full_name = f"{PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


DOMAIN = _load_module("const").DOMAIN
runtime_data = types.ModuleType(f"{PKG_NAME}.runtime_data")
runtime_data.call_projection = lambda hass: hass.data.get(DOMAIN, {}).get(
    "call_registry"
)
runtime_data.endpoint_directory = lambda hass: hass.data.get(DOMAIN, {}).get(
    "endpoint_registry",
    SimpleNamespace(get=lambda _endpoint_id: None),
)
runtime_data.sip_endpoint_manager = lambda hass: hass.data.get(DOMAIN, {}).get(
    "sip_endpoint"
)
runtime_data.sip_trunk = lambda hass: hass.data.get(DOMAIN, {}).get("sip_trunk")
sys.modules[runtime_data.__name__] = runtime_data
sip_runtime = _load_module("sip_runtime")
enable_reused_tcp_connection = sip_runtime.enable_reused_tcp_connection
send_bye = sip_runtime.send_bye
send_final_response = sip_runtime.send_final_response
sip_servers = sip_runtime.sip_servers
uri_transport = sip_runtime.uri_transport


class _Server:
    def __init__(self, *, owns: str = "") -> None:
        self.owns = owns
        self.final_calls: list[tuple] = []
        self.bye_calls: list[str] = []

    def send_final_response(self, call_id, status, reason, **kwargs):
        self.final_calls.append((call_id, status, reason, kwargs))
        return call_id == self.owns

    def send_bye(self, call_id):
        self.bye_calls.append(call_id)
        return call_id == self.owns


class _TcpServer:
    def __init__(self) -> None:
        self.opened: list[tuple[tuple[str, int], str]] = []
        self.closed: list[tuple[tuple[str, int], str]] = []
        self.send = object()
        self.responses = object()

    def open_reused_dialog(self, addr, call_id):
        self.opened.append((addr, call_id))
        return self.send, self.responses

    def close_reused_dialog(self, addr, call_id):
        self.closed.append((addr, call_id))


class SipRuntimeTest(unittest.TestCase):
    def test_server_discovery_prefers_manager_and_includes_trunk(self) -> None:
        manager = object()
        legacy_udp = object()
        legacy_tcp = object()
        trunk_endpoint = object()
        hass = SimpleNamespace(
            data={
                DOMAIN: {
                    "sip_endpoint": manager,
                    "sip_server": legacy_udp,
                    "sip_tcp_server": legacy_tcp,
                    "sip_trunk": SimpleNamespace(inbound_endpoint=trunk_endpoint),
                }
            }
        )

        self.assertEqual(sip_servers(hass), [manager, trunk_endpoint])

    def test_final_response_and_bye_stop_at_dialog_owner(self) -> None:
        first = _Server()
        owner = _Server(owns="call-1")
        hass = SimpleNamespace(
            data={DOMAIN: {"sip_server": first, "sip_tcp_server": owner}}
        )

        self.assertTrue(
            send_final_response(
                hass,
                "call-1",
                200,
                "OK",
                answer_sdp="v=0",
            )
        )
        self.assertTrue(send_bye(hass, "call-1"))
        self.assertEqual(len(first.final_calls), 1)
        self.assertEqual(len(owner.final_calls), 1)
        self.assertEqual(len(first.bye_calls), 1)
        self.assertEqual(len(owner.bye_calls), 1)

    def test_success_response_resolves_answering_endpoint_identity(self) -> None:
        owner = _Server(owns="call-identity")
        session = SimpleNamespace(
            metadata={"dest_endpoint_id": "kitchen"},
            callee="427",
        )
        call_registry = SimpleNamespace(
            resolve_session_id=lambda call_id: call_id,
            sessions={"call-identity": session},
        )
        endpoint = SimpleNamespace(name="Cucina", sip_uri_user="428")
        endpoint_registry = SimpleNamespace(
            get=lambda endpoint_id: endpoint if endpoint_id == "kitchen" else None
        )
        hass = SimpleNamespace(
            data={
                DOMAIN: {
                    "sip_server": owner,
                    "call_registry": call_registry,
                    "endpoint_registry": endpoint_registry,
                }
            }
        )

        self.assertTrue(
            send_final_response(
                hass,
                "call-identity",
                200,
                "OK",
                answer_sdp="v=0",
            )
        )
        kwargs = owner.final_calls[-1][3]
        self.assertEqual(kwargs["connected_identity_name"], "Cucina")
        self.assertEqual(kwargs["connected_identity_user"], "428")

    def test_failure_response_does_not_publish_connected_identity(self) -> None:
        owner = _Server(owns="call-failure")
        hass = SimpleNamespace(
            data={
                DOMAIN: {
                    "sip_server": owner,
                    "call_registry": SimpleNamespace(
                        resolve_session_id=lambda call_id: call_id,
                        sessions={
                            "call-failure": SimpleNamespace(
                                metadata={"connected_party": "Cucina"},
                                callee="427",
                            )
                        },
                    ),
                }
            }
        )

        self.assertTrue(
            send_final_response(hass, "call-failure", 486, "Busy Here")
        )
        kwargs = owner.final_calls[-1][3]
        self.assertEqual(kwargs["connected_identity_name"], "")
        self.assertEqual(kwargs["connected_identity_user"], "")

    def test_uri_transport_defaults_to_udp(self) -> None:
        self.assertEqual(uri_transport(SimpleNamespace(params=())), "UDP")
        self.assertEqual(
            uri_transport(SimpleNamespace(params=(("transport", "tcp"),))),
            "TCP",
        )

    def test_tcp_contact_reuses_registered_flow_and_releases_dialog(self) -> None:
        tcp_server = _TcpServer()
        hass = SimpleNamespace(
            data={DOMAIN: {"sip_endpoint": SimpleNamespace(tcp_server=tcp_server)}}
        )
        client = SimpleNamespace(
            dialog_ids=SimpleNamespace(call_id="call-2"),
            use_reused_tcp_connection=lambda **kwargs: setattr(
                client,
                "reused",
                kwargs,
            ),
        )
        uri = SimpleNamespace(
            host="192.0.2.10",
            port=5090,
            params=(("transport", "tcp"),),
        )

        self.assertTrue(
            enable_reused_tcp_connection(
                hass,
                client,
                uri,
                target="Desk",
                default_sip_port=5060,
            )
        )
        self.assertEqual(tcp_server.opened, [(('192.0.2.10', 5090), 'call-2')])
        self.assertIs(client.reused["send"], tcp_server.send)
        self.assertIs(client.reused["responses"], tcp_server.responses)
        client.reused["close"]()
        self.assertEqual(tcp_server.closed, [(('192.0.2.10', 5090), 'call-2')])

    def test_udp_contact_does_not_attempt_tcp_reuse(self) -> None:
        hass = SimpleNamespace(data={DOMAIN: {}})
        client = SimpleNamespace(dialog_ids=SimpleNamespace(call_id="call-3"))
        uri = SimpleNamespace(host="192.0.2.11", port=None, params=())

        self.assertFalse(
            enable_reused_tcp_connection(
                hass,
                client,
                uri,
                target="Desk",
                default_sip_port=5060,
            )
        )


if __name__ == "__main__":
    unittest.main()
