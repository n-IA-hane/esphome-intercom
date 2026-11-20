"""SIP messages for a fixture peer retrying its own initial INVITE after a real challenge."""

import asyncio

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.sip_auth import build_digest_authorization


def authenticated_invite_retry(request, challenge, username, password):
    """Build the non-2xx ACK and a new signed transaction with unchanged media."""
    assert request.method == "INVITE" and challenge.status_code == 401
    assert (challenge.header("Call-ID"), challenge.header("CSeq")) == (request.header("Call-ID"), request.header("CSeq"))
    seq = sip.parse_cseq(request.header("CSeq")).number
    via = request.header("Via")
    original_branch = sip.parse_via(via).branch
    assert original_branch and original_branch in via
    ack_headers = [(key, value) for key, value in request.headers if key.lower() in
                   {"via", "from", "call-id", "route", "max-forwards"}]
    ack_headers.extend((("To", challenge.header("To")), ("CSeq", f"{seq} ACK")))
    headers = []
    for key, value in request.headers:
        name = key.lower()
        if name in {"authorization", "content-length"}:
            continue
        if name == "via":
            value = value.replace(original_branch, sip.make_branch(), 1)
        elif name == "cseq":
            value = f"{seq + 1} INVITE"
        headers.append((key, value))
    digest = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
        username=username, password=password, method="INVITE", uri=request.uri, body=request.body)
    headers.append(("Authorization", digest))
    return sip.build_request("ACK", request.uri, ack_headers), sip.build_request("INVITE", request.uri, headers, request.body)

async def send_authenticated_invite(transport, listener, raw, receive, username, password):
    """Perform the challenge exchange on the peer's own signaling socket."""
    initial = sip.parse_message(raw)
    transport.sendto(raw, listener)
    async with asyncio.timeout(3):
        while True:
            response, _ = await receive()
            if (response.is_response and response.header("Call-ID") == initial.header("Call-ID")
                    and response.header("CSeq") == initial.header("CSeq") and response.status_code >= 200):
                break
    ack, retry = authenticated_invite_retry(initial, response, username, password)
    transport.sendto(ack, listener)
    transport.sendto(retry, listener)
    return sip.parse_message(retry)
