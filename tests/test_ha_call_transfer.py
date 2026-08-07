"""Established-call transfer policy with the real Home Assistant imports."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.voip_stack import call_transfer
from custom_components.voip_stack.core import sdp, sip_transfer
from custom_components.voip_stack.sip_client import (
    SipCallClient,
    SipDialog,
    SipTransferResult,
)


pytestmark = pytest.mark.ha


def _established(call_id: str, remote_tag: str, user: str) -> SipCallClient:
    pcm = sdp.RtpPcmFormat(96, "L16", 16000, 1, 32)
    client = SipCallClient(
        local_ip="127.0.0.1",
        local_name="HA",
        local_sip_port=5060,
        local_rtp_port=41000,
    )
    client.dialog_ids.call_id = call_id
    client.dialog_ids.remote_tag = remote_tag
    client.dialog = SipDialog(
        target=user,
        remote_host="127.0.0.2",
        remote_sip_port=5060,
        remote_rtp_host="127.0.0.2",
        remote_rtp_port=42000,
        local_rtp_port=41000,
        call_id=call_id,
        remote_tag=remote_tag,
        local_uri="sip:HA@127.0.0.1:5060",
        remote_uri=f"sip:{user}@127.0.0.2:5060",
        send_format=pcm,
        recv_format=pcm,
    )
    return client


async def test_attended_transfer_uses_the_consultation_dialog_identity() -> None:
    source = _established("source", "source-remote", "source")
    consultation = _established("consult", "consult-remote", "desk")
    observed: list[sip_transfer.SipReferTarget] = []

    async def refer(target: sip_transfer.SipReferTarget) -> SipTransferResult:
        observed.append(target)
        return SipTransferResult(True, 200, "completed")

    source.refer = refer  # type: ignore[method-assign]
    runtime = SimpleNamespace(
        sip=SimpleNamespace(
            sip_clients_snapshot=lambda: {
                "source": source,
                "consult": consultation,
            },
        ),
        endpoints=SimpleNamespace(resolve=lambda _value: None),
    )

    result = await call_transfer.async_transfer_call(
        runtime,
        call_transfer.CallTransferRequest(
            call_id="source",
            destination="",
            replaces_call_id="consult",
        ),
    )

    assert result.accepted
    assert observed == [
        sip_transfer.SipReferTarget(
            "sip:desk@127.0.0.2:5060",
            sip_transfer.SipReplaces(
                "consult",
                to_tag="consult-remote",
                from_tag=consultation.dialog_ids.local_tag,
            ),
        )
    ]
