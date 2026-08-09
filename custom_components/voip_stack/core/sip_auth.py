"""SIP digest authentication helpers."""

from __future__ import annotations

import hashlib
import hmac
import re
from secrets import token_hex
from typing import Protocol


_PARAM_RE = re.compile(r'([a-zA-Z0-9_-]+)=("([^"\\]*(?:\\.[^"\\]*)*)"|[^,\s]+)')


_HASH_NAMES = {
    "MD5": "md5",
    "SHA-256": "sha256",
    "SHA-512-256": "sha512_256",
}
_ALGORITHM_PREFERENCE = {
    "MD5": 1,
    "SHA-256": 2,
    "SHA-512-256": 3,
}


class DigestChallengeResponse(Protocol):
    status_code: int | None

    def header_values(self, name: str) -> list[str]: ...


class DigestChallengeTracker:
    """Bound authentication retries independently per credential scope."""

    __slots__ = ("attempts",)

    def __init__(self) -> None:
        self.attempts: dict[str, int] = {}

    def claim(self, scope: str, values: list[str] | tuple[str, ...]) -> str:
        challenge = select_digest_challenge(values)
        stale = parse_digest_challenge(challenge).get(
            "stale", "false"
        ).lower() == "true"
        attempts = self.attempts.get(scope, 0)
        if attempts and (attempts >= 2 or not stale):
            raise ValueError("SIP digest credentials were rejected")
        self.attempts[scope] = attempts + 1
        return challenge

    def authorize(
        self,
        response: DigestChallengeResponse,
        *,
        username: str,
        password: str,
        method: str,
        uri: str,
        auth_username: str = "",
        nonce_count: int = 1,
        body: str | bytes = b"",
    ) -> tuple[str, str, str]:
        """Build one bounded Authorization response for a 401 or 407."""

        proxy = response.status_code == 407
        header = "Proxy-Authorization" if proxy else "Authorization"
        challenge = self.claim(
            header,
            response.header_values(
                "Proxy-Authenticate" if proxy else "WWW-Authenticate"
            ),
        )
        return (
            header,
            challenge,
            build_digest_authorization(
                challenge_header=challenge,
                username=username,
                auth_username=auth_username,
                password=password,
                method=method,
                uri=uri,
                nonce_count=nonce_count,
                body=body,
            ),
        )


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


def select_digest_challenge(
    values: list[str] | tuple[str, ...],
    *,
    realm: str = "",
) -> str:
    """Select the strongest compatible Digest challenge deterministically."""

    selected = ""
    selected_rank = -1
    for value in values:
        raw = str(value or "").strip()
        if not raw.lower().startswith("digest "):
            continue
        params = parse_digest_challenge(raw)
        if not params.get("realm") or not params.get("nonce"):
            continue
        if realm and params["realm"] != realm:
            continue
        algorithm = (params.get("algorithm") or "MD5").upper()
        rank = _ALGORITHM_PREFERENCE.get(algorithm.removesuffix("-SESS"), -1)
        qops = {
            part.strip().lower()
            for part in params.get("qop", "").split(",")
            if part.strip()
        }
        if rank < 0 or (qops and qops.isdisjoint({"auth", "auth-int"})):
            continue
        if rank > selected_rank:
            selected = raw
            selected_rank = rank
    if not selected:
        raise ValueError("no compatible SIP digest challenge")
    return selected


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
    if opaque := challenge.get("opaque", ""):
        params["opaque"] = opaque
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


def verify_digest_authorization(
    *,
    authorization_header: str,
    username: str,
    password: str,
    method: str,
    uri: str,
    realm: str,
    nonce: str,
    body: str | bytes = b"",
) -> tuple[str, str, int]:
    """Verify one Digest response and return algorithm, cnonce and nonce-count."""

    params = parse_digest_challenge(authorization_header)
    algorithm = (params.get("algorithm") or "MD5").upper()
    qop = params.get("qop", "").lower()
    cnonce = params.get("cnonce", "")
    nc = params.get("nc", "")
    if (
        params.get("username", "").casefold() != username.casefold()
        or params.get("realm") != realm
        or params.get("nonce") != nonce
        or params.get("uri") != uri
        or algorithm.removesuffix("-SESS") not in _HASH_NAMES
        or qop not in {"auth", "auth-int"}
        or not cnonce
        or len(cnonce) > 128
        or len(nc) != 8
    ):
        raise ValueError("invalid SIP digest authorization parameters")
    try:
        nonce_count = int(nc, 16)
    except ValueError as err:
        raise ValueError("invalid SIP digest nonce-count") from err
    if nonce_count < 1:
        raise ValueError("invalid SIP digest nonce-count")
    challenge = (
        f'Digest realm="{realm}", nonce="{nonce}", '
        f'algorithm={algorithm}, qop="{qop}"'
    )
    expected = parse_digest_challenge(
        build_digest_authorization(
            challenge_header=challenge,
            username=username,
            password=password,
            method=method,
            uri=uri,
            nonce_count=nonce_count,
            cnonce=cnonce,
            body=body,
        )
    ).get("response", "")
    if not hmac.compare_digest(expected, params.get("response", "")):
        raise ValueError("invalid SIP digest response")
    return algorithm, cnonce, nonce_count
