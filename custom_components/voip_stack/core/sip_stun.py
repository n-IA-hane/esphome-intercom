"""Stateless RFC 5626 section 8 keep-alives on the SIP UDP transport.

This usage has no STUN credentials or allocation: the response reports only the
observed source tuple. It never changes the separately authenticated SIP lease.
"""

from __future__ import annotations

import ipaddress
import struct
import zlib


_MAGIC_COOKIE = 0x2112A442
_FINGERPRINT = 0x8028
# RFC 5389 attributes known but unexpected in this unauthenticated usage are
# ignored per section 7.3. Unknown required attributes instead receive 420.
_KNOWN_REQUIRED = frozenset({0x0001, 0x0006, 0x0008, 0x0009, 0x000A, 0x0014, 0x0015, 0x0020})


def is_stun_message(data: bytes) -> bool:
    """Recognize RFC 5389 UDP framing, without authenticating its attributes."""
    if len(data) < 20:
        return False
    kind, length, cookie = struct.unpack_from("!HHI", data)
    return kind & 0xC000 == 0 and cookie == _MAGIC_COOKIE and length % 4 == 0 and len(data) == 20 + length


def binding_keepalive_response(data: bytes, address: tuple) -> bytes | None:
    """Return a limited Binding response, or discard invalid/non-request data."""

    if not is_stun_message(data) or struct.unpack_from("!H", data)[0] != 0x0001:
        return None
    unknown = set()
    integrity_seen = False
    offset = 20
    while offset < len(data):
        if offset + 4 > len(data):
            return None
        attribute, size = struct.unpack_from("!HH", data, offset)
        end = offset + 4 + ((size + 3) & ~3)
        if end > len(data):
            return None
        if attribute == _FINGERPRINT:
            if size != 4 or end != len(data):
                return None
            expected = zlib.crc32(data[:offset]) ^ 0x5354554E
            if struct.unpack_from("!I", data, offset + 4)[0] != expected:
                return None
        elif integrity_seen:
            # RFC 5389 section 15.4: only FINGERPRINT is processed after MI.
            pass
        elif attribute < 0x8000 and attribute not in _KNOWN_REQUIRED:
            unknown.add(attribute)
        if attribute == 0x0008 and not integrity_seen:
            # A malformed MI is not the boundary defined by RFC 5389 15.4.
            if size != 20:
                return None
            integrity_seen = True
        offset = end

    if unknown:
        # RFC 5389 section 7.3.1: no success for an unknown required attribute.
        reason = b"Unknown Attribute"
        error = struct.pack("!HH", 0x0009, 4 + len(reason)) + b"\0\0\x04\x14" + reason
        error += b"\0" * (-len(error) % 4)
        values = b"".join(struct.pack("!H", item) for item in sorted(unknown))
        payload = error + struct.pack("!HH", 0x000A, len(values)) + values
        payload += b"\0" * (-len(payload) % 4)
        response_type = 0x0111
    else:
        try:
            ip = ipaddress.ip_address(address[0].split("%", 1)[0])
            port = int(address[1])
        except (ValueError, TypeError, IndexError, AttributeError):
            return None
        if not 0 < port <= 65535:
            return None
        mask = struct.pack("!I", _MAGIC_COOKIE) + data[8:20]
        mapped = bytes(value ^ mask[index] for index, value in enumerate(ip.packed))
        value = struct.pack("!BBH", 0, 1 if ip.version == 4 else 2, port ^ 0x2112) + mapped
        payload = struct.pack("!HH", 0x0020, len(value)) + value
        response_type = 0x0101
    # Including FINGERPRINT also lets a receiver independently demultiplex us.
    response = struct.pack("!HHI", response_type, len(payload) + 8, _MAGIC_COOKIE) + data[8:20] + payload
    return response + struct.pack("!HHI", _FINGERPRINT, 4, zlib.crc32(response) ^ 0x5354554E)
