"""SIP digest authentication helpers."""

from __future__ import annotations

import hashlib
import re
from secrets import token_hex


_PARAM_RE = re.compile(r'([a-zA-Z0-9_-]+)=("([^"\\]*(?:\\.[^"\\]*)*)"|[^,\s]+)')


_HASH_NAMES = {
    "MD5": "md5",
    "SHA-256": "sha256",
    "SHA-512-256": "sha512_256",
}


def _digest(value: str | bytes, algorithm: str) -> str:
    data = value.encode() if isinstance(value, str) else value
    name = _HASH_NAMES.get(algorithm.removesuffix("-SESS"))
    if name is None:
        raise ValueError(f"unsupported SIP digest algorithm {algorithm}")
    return hashlib.new(name, data, usedforsecurity=False).hexdigest()


def sip_digest_md5(value: str) -> str:
    """Return the legacy MD5 hex used by existing registrar callers."""

    return _digest(value, "MD5")


def parse_digest_challenge(value: str) -> dict[str, str]:
    raw = (value or "").strip()
    if raw.lower().startswith("digest "):
        raw = raw[7:].strip()
    out: dict[str, str] = {}
    for match in _PARAM_RE.finditer(raw):
        key = match.group(1).lower()
        val = match.group(3) if match.group(3) is not None else match.group(2)
        out[key] = val.replace('\\"', '"') if val is not None else ""
    return out


def build_digest_authorization(
    *,
    challenge_header: str,
    username: str,
    password: str,
    method: str,
    uri: str,
    auth_username: str = "",
    nonce_count: int = 1,
    cnonce: str = "",
    body: str | bytes = b"",
) -> str:
    challenge = parse_digest_challenge(challenge_header)
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    algorithm = (challenge.get("algorithm") or "MD5").upper()
    qop_raw = challenge.get("qop", "")
    qops = [part.strip().lower() for part in qop_raw.split(",") if part.strip()]
    if qops and not {"auth", "auth-int"}.intersection(qops):
        raise ValueError(f"unsupported SIP digest qop {','.join(qops)}")
    qop = "auth" if "auth" in qops else ("auth-int" if qops else "")
    digest_user = auth_username or username
    base_algorithm = algorithm.removesuffix("-SESS")
    ha1 = _digest(f"{digest_user}:{realm}:{password}", base_algorithm)
    if algorithm.endswith("-SESS"):
        cnonce = cnonce or token_hex(8)
        ha1 = _digest(f"{ha1}:{nonce}:{cnonce}", base_algorithm)
    entity = f":{_digest(body, base_algorithm)}" if qop == "auth-int" else ""
    ha2 = _digest(f"{method.upper()}:{uri}{entity}", base_algorithm)
    params = {
        "username": digest_user,
        "realm": realm,
        "nonce": nonce,
        "uri": uri,
        "response": "",
        "algorithm": algorithm,
    }
    if qop:
        if int(nonce_count) < 1:
            raise ValueError("SIP digest nonce_count must be positive")
        cnonce = cnonce or token_hex(8)
        nc = f"{int(nonce_count):08x}"
        response = _digest(
            f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}",
            base_algorithm,
        )
        params.update({"qop": qop, "nc": nc, "cnonce": cnonce, "response": response})
    else:
        params["response"] = _digest(f"{ha1}:{nonce}:{ha2}", base_algorithm)
    rendered = []
    for key, val in params.items():
        if key in {"algorithm", "qop", "nc"}:
            rendered.append(f"{key}={val}")
        else:
            rendered.append(f'{key}="{str(val).replace(chr(34), "")}"')
    return "Digest " + ", ".join(rendered)
