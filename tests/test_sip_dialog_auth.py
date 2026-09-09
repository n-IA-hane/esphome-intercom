"""Real UDP dialog termination against an independently checking provider."""

import asyncio
import hashlib
import re

import pytest
from .voip_phase1_support import sip_client, sip, sdp


class Provider(asyncio.DatagramProtocol):
    def __init__(self, challenge=0, delay=0, reject=False):
        self.reject = reject
        self.challenge = challenge
        self.delay = delay
        self.accepted = False
        self.received = []
        self.tasks = []

    def connection_made(self, t):
        self.transport = t

    def datagram_received(self, data, addr):
        m = sip.parse_message(data)
        self.received.append(m)

        async def respond():
            await asyncio.sleep(self.delay)
            authorized = m.header("Authorization") or m.header("Proxy-Authorization")
            for value in [m.header("Authorization"), m.header("Proxy-Authorization")]:
                if not value:
                    continue
                fields = {}
                for match in re.finditer(r'(\w+)=(?:"([^"]*)"|([^, ]+))', value):
                    fields[match[1]] = match[2] if match[2] is not None else match[3]

                def md5(value):
                    return hashlib.md5(value.encode()).hexdigest()

                expected = md5(
                    md5("test-user:test-provider:test-password")
                    + ":test-nonce:"
                    + fields["nc"]
                    + ":"
                    + fields["cnonce"]
                    + ":auth:"
                    + md5(m.method + ":" + m.uri)
                )
                assert fields["response"] == expected and fields["uri"] == m.uri
            if isinstance(self.challenge, tuple):
                code = next(
                    (
                        code
                        for code in self.challenge
                        if not m.header(
                            "Authorization" if code == 401 else "Proxy-Authorization"
                        )
                    ),
                    200,
                )
            else:
                code = (
                    self.challenge
                    if self.challenge and (self.reject or not authorized)
                    else 200
                )
            hs = [
                (key, val)
                for key in ["Via", "From", "To", "Call-ID", "CSeq"]
                for val in m.header_values(key)
            ]
            if code in (401, 407):
                hs.append(
                    (
                        "WWW-Authenticate" if code == 401 else "Proxy-Authenticate",
                        'Digest realm="test-provider", nonce="test-nonce", algorithm=MD5, qop="auth"',
                    )
                )
            else:
                self.accepted = True
            self.transport.sendto(
                sip.build_response(
                    code, "OK" if code == 200 else "Authentication required", hs
                ),
                addr,
            )

        self.tasks.append(asyncio.create_task(respond()))


async def case(challenge, delay, reject=False, method="BYE"):
    loop = asyncio.get_running_loop()
    p = Provider(challenge, delay, reject)
    transport, _ = await loop.create_datagram_endpoint(
        lambda: p, local_addr=("127.0.0.1", 0)
    )
    port = transport.get_extra_info("sockname")[1]
    c = sip_client.SipCallClient(
        local_ip="127.0.0.1",
        local_name="HA",
        local_sip_port=0,
        local_rtp_port=41000,
        username="test-user",
        password="test-password",
    )
    await c.start()
    c.dialog_ids.remote_tag = "test-remote"
    fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    c.dialog = sip_client.SipDialog(
        target="test",
        remote_host="127.0.0.1",
        remote_sip_port=port,
        remote_rtp_host="127.0.0.1",
        remote_rtp_port=42000,
        local_rtp_port=41000,
        call_id=c.dialog_ids.call_id,
        local_uri="sip:test-user@127.0.0.1",
        remote_uri="sip:peer@127.0.0.1",
        send_format=fmt,
        recv_format=fmt,
        remote_target_uri=f"sip:peer@127.0.0.1:{port}",
    )
    try:
        outcome = (
            await c._terminate_confirmed_dialog(timeout=1.5)
            if method == "BYE"
            else await c.send_dtmf_info("5", timeout=1.5)
        )
        return dict(
            challenge=challenge,
            delay=delay,
            result=outcome,
            remote_terminated=p.accepted,
            requests=[m.method for m in p.received],
            cseqs=[m.header("CSeq") for m in p.received],
            vias=[m.header("Via") for m in p.received],
            local_dialog_cleared=c.dialog is None,
        )
    finally:
        await c.close()
        await asyncio.gather(*p.tasks)
        transport.close()


@pytest.mark.parametrize("challenge,delay", [(0, 0), (0, 0.7), (401, 0), (407, 0)])
def test_bye_provider_confirms_remote_termination(challenge, delay):
    result = asyncio.run(case(challenge, delay))
    assert result["remote_terminated"], result
    assert result["local_dialog_cleared"]
    if challenge:
        assert len(result["requests"]) == 2
        assert result["cseqs"][0] != result["cseqs"][1]
        assert result["vias"][0] != result["vias"][1]


@pytest.mark.parametrize("challenge", [401, 407])
def test_rejected_bye_credentials_stop_retrying(challenge):
    result = asyncio.run(case(challenge, 0, reject=True))
    assert not result["remote_terminated"]
    assert result["result"] == (
        "auth_required_unsupported"
        if challenge == 401
        else "proxy_auth_required_unsupported"
    )
    assert len(result["requests"]) == 2
    assert result["local_dialog_cleared"]


@pytest.mark.parametrize("challenge", [401, 407])
def test_dtmf_info_challenge_preserves_active_dialog(challenge):
    result = asyncio.run(case(challenge, 0, method="INFO"))
    assert result["result"] is True
    assert not result["local_dialog_cleared"]
    assert result["requests"] == ["INFO", "INFO"]


@pytest.mark.parametrize("challenges", [(401, 407), (407, 401)])
def test_proxy_and_server_challenges_preserve_both_credentials(challenges):
    result = asyncio.run(case(challenges, 0))
    assert result["remote_terminated"]
    assert result["requests"] == ["BYE"] * 3
    assert len(set(result["cseqs"])) == 3
    assert len(set(result["vias"])) == 3
