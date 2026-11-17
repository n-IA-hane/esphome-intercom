"""Advertised media destinations, separate from the sockets' local bindings."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address
from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class MediaEndpoint:
    local_port: int
    host: str
    port: int

    def __post_init__(self) -> None:
        address = IPv4Address(self.host)
        if address.is_unspecified or address.is_multicast or not 0 < self.port < 65536:
            raise ValueError("invalid advertised media destination")


def apply_media_endpoints(body: str, endpoints: Mapping[str, MediaEndpoint]) -> str:
    """Change only active media destinations in a locally generated SDP.

    Each stream can have a different public binding. In particular, NAT does
    not guarantee that video RTCP is RTP+1 (RFC 3605). Origin, codecs, media
    ordering, directions and rejected sections retain their original values.
    A zero connection address, including session inheritance, keeps its
    no-RTP/no-RTCP meaning (RFC 3264 section 8.4). With NAT enabled every
    other audio/video section needs its own known binding, even if inactive.
    """
    if not endpoints:
        return body
    sections: list[list[str]] = [[]]
    for line in body.splitlines():
        if line.startswith("m="):
            sections.append([])
        sections[-1].append(line)
    session_connection = next((line for line in sections[0] if line.startswith("c=")), "")
    seen: set[str] = set()
    for section in sections[1:]:
        words = section[0].split()
        kind = words[0][2:]
        connection = next((line for line in section[1:] if line.startswith("c=")), session_connection)
        if (kind not in {"audio", "video"} or words[1].split("/")[0] == "0"
                or connection.split() == ["c=IN", "IP4", "0.0.0.0"]):
            continue
        endpoint = endpoints.get(kind)
        if endpoint is None or kind in seen:
            raise ValueError("Active media requires its own NAT binding")
        if int(words[1]) not in {endpoint.local_port, endpoint.port}:
            raise ValueError("Changed media socket requires a fresh NAT binding")
        seen.add(kind)
        words[1] = str(endpoint.port)
        section[0] = " ".join(words)
        section[1:] = [line for line in section[1:] if not line.startswith("c=")]
        section.insert(1, f"c=IN IP4 {endpoint.host}")
        rtcp = endpoints.get(kind + "_rtcp")
        if rtcp is not None:
            section[:] = [line for line in section if not line.startswith("a=rtcp:")]
            section.append(f"a=rtcp:{rtcp.port} IN IP4 {rtcp.host}")
    return "\r\n".join(line for section in sections for line in section) + "\r\n"
