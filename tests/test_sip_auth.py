"""Standards-based SIP Digest authentication tests."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sip_auth_under_test",
    ROOT / "custom_components/voip_stack/core/sip_auth.py",
)
assert SPEC is not None and SPEC.loader is not None
sip_auth = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sip_auth)
build_digest_authorization = sip_auth.build_digest_authorization
parse_digest_challenge = sip_auth.parse_digest_challenge


def _hash(value: str | bytes, algorithm: str) -> str:
    data = value.encode() if isinstance(value, str) else value
    return hashlib.new(algorithm, data, usedforsecurity=False).hexdigest()


@pytest.mark.parametrize(
    ("token", "hash_name"),
    (("SHA-256", "sha256"), ("SHA-512-256", "sha512_256")),
)
def test_modern_digest_algorithms(token: str, hash_name: str) -> None:
    header = build_digest_authorization(
        challenge_header=(
            f'Digest realm="pbx", nonce="nonce", algorithm={token}, qop="auth"'
        ),
        username="alice",
        password="secret",
        method="REGISTER",
        uri="sip:pbx.local",
        nonce_count=1,
        cnonce="client",
    )
    params = parse_digest_challenge(header)
    ha1 = _hash("alice:pbx:secret", hash_name)
    ha2 = _hash("REGISTER:sip:pbx.local", hash_name)
    expected = _hash(f"{ha1}:nonce:00000001:client:auth:{ha2}", hash_name)

    assert params["algorithm"] == token
    assert params["response"] == expected


def test_auth_int_and_session_algorithm_hash_the_entity() -> None:
    header = build_digest_authorization(
        challenge_header=(
            'Digest realm="pbx", nonce="nonce", algorithm=SHA-256-SESS, '
            'qop="auth-int"'
        ),
        username="alice",
        password="secret",
        method="INVITE",
        uri="sip:bob@pbx.local",
        nonce_count=2,
        cnonce="client",
        body="v=0\r\n",
    )
    params = parse_digest_challenge(header)
    initial = _hash("alice:pbx:secret", "sha256")
    ha1 = _hash(f"{initial}:nonce:client", "sha256")
    entity = _hash("v=0\r\n", "sha256")
    ha2 = _hash(f"INVITE:sip:bob@pbx.local:{entity}", "sha256")
    expected = _hash(
        f"{ha1}:nonce:00000002:client:auth-int:{ha2}",
        "sha256",
    )

    assert params["algorithm"] == "SHA-256-SESS"
    assert params["qop"] == "auth-int"
    assert params["response"] == expected


def test_unknown_digest_algorithm_fails_explicitly() -> None:
    with pytest.raises(ValueError, match="unsupported SIP digest algorithm"):
        build_digest_authorization(
            challenge_header='Digest realm="pbx", nonce="n", algorithm=SHA-1',
            username="alice",
            password="secret",
            method="REGISTER",
            uri="sip:pbx.local",
        )
