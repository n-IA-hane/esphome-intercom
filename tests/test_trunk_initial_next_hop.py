"""Initial trunk transport discovery is independent of logical URI and dialog routes."""

import pytest

from .voip_phase1_support import _load_intercom_module, sip_client, sip_trunk, sip_resolution

trunk_signaling = _load_intercom_module("trunk_signaling")


@pytest.mark.parametrize("transport", ["UDP", "TCP", "TLS"])
@pytest.mark.parametrize("proxy", [False, True])
async def test_unregistered_trunk_resolves_server_or_proxy_without_resolving_called_domain(monkeypatch, transport, proxy):
    requested = []
    exchanges = []
    proxy_uri = "sip:proxy.example:5088" if proxy else ""
    class Resolver:
        async def resolve(self, uri, *, transport):
            requested.append((uri.host, uri.port, transport))
            return (sip_resolution.SipServerTarget(uri.host, uri.port, transport, ("192.0.2.10",)),)
    trunk = sip_trunk.SipTrunkClient(config=sip_trunk.SipTrunkConfig(options_interval=30,
        enabled=True, transport=transport.lower(), server="server.example", port=5099,
        domain="called.example", username="line", auth_username="", password="", expires=300,
        outbound_proxy=proxy_uri), local_ip="127.0.0.1", local_sip_port=5060)
    client = sip_client.SipCallClient(local_ip="127.0.0.1", local_name="Caller", local_sip_port=5060,
        local_rtp_port=41000, signaling_transport=transport, outbound_proxy=proxy_uri, target_resolver=Resolver())
    assert not trunk_signaling.reuse_registered_trunk_flow(trunk, client)
    async def stop_before_connect(exchange):
        exchanges.append((exchange.request_uri, exchange.endpoints))
        return False
    monkeypatch.setattr(client, "_select_next_invite_target", stop_before_connect)
    try:
        await client.invite(target="123", remote_host="called.example", remote_sip_port=5099,
            request_uri="sip:123@called.example:5099", timeout=1)
        selected = ("proxy.example", 5088) if proxy else ("server.example", 5099)
        assert requested == [(*selected, transport)]
        assert exchanges == [("sip:123@called.example:5099", (("192.0.2.10", selected[1], transport),))]
        assert client._tls_server_name == selected[0]
    finally:
        await client.close()
