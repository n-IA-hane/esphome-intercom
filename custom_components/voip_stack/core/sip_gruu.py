"""RFC 5627 instance routing tokens, separate from authentication credentials."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import sip


class GruuTokens:
    """Temporary tokens carry encrypted binding identity without an alias cache."""

    def __init__(self, key: bytes | None = None):
        self._key = key if key is not None else AESGCM.generate_key(bit_length=128)
        self._cipher = AESGCM(self._key)

    @property
    def persistence_key(self) -> bytes:
        """Return the instance-routing key to the registrar's private store."""
        return self._key

    @staticmethod
    def public_id(account: str, instance: str) -> str:
        return hashlib.sha256((account.casefold() + "\x00" + instance).encode()).hexdigest()

    def issue(self, aor: sip.SipUri, instance: str, epoch: str) -> tuple[str, str]:
        public = sip.SipUri(aor.user, aor.host, aor.port,
                            params=(("gr", self.public_id(aor.user, instance)),), scheme=aor.scheme)
        nonce = secrets.token_bytes(12)
        payload = json.dumps([aor.user, instance, epoch], separators=(",", ":")).encode()
        token = base64.urlsafe_b64encode(nonce + self._cipher.encrypt(nonce, payload, b"sip-gruu")).decode().rstrip("=")
        temporary = sip.SipUri("tgruu-" + token, aor.host, aor.port, params=(("gr", None),), scheme=aor.scheme)
        return str(public), str(temporary)

    def temporary_identity(self, uri: sip.SipUri) -> tuple[str, str, str] | None:
        if not uri.user.startswith("tgruu-") or len(uri.user) > 2048:
            return None
        try:
            token = uri.user.removeprefix("tgruu-")
            data = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            payload = self._cipher.decrypt(data[:12], data[12:], b"sip-gruu")
            values = json.loads(payload)
            if len(values) == 3 and all(isinstance(value, str) for value in values):
                return tuple(values)
        except Exception:
            return None
        return None
