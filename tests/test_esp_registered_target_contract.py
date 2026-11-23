"""HA and the ESP must agree on how a registered ESP line is addressed.

HA resolves a peer to a Request-URI user (SIP username, else extension, else
its text name). The ESP decides admission from that user and the registrar
that owns the line. Each side was green alone while real calls were rejected,
so this joins HA's actual URI builder to the ESP's actual registration engine.
"""

from pathlib import Path
import os
import subprocess
import sys

import pytest

from tests.test_video_rtp import _load

ROOT = Path(__file__).resolve().parents[1]
VOIP = Path(os.environ.get("VOIP_STACK_REPO", ROOT.parent / "esphome-voip-stack"))
SOURCE = VOIP / "esphome/components/voip_stack"

peer_module = _load("peer")
sip = sys.modules[peer_module.SipUri.__module__]


def wire_user(**fields) -> str:
    """Return the Request-URI user exactly as HA serializes it for one ESP peer."""
    peer = peer_module.Peer(host="192.0.2.10", endpoint_kind="esphome", **fields)
    wire = str(peer_module.sip_uri_for_peer(peer, default_port=5060))
    assert sip.parse_sip_uri(wire).user == peer.request_uri_user
    return wire.split(":", 1)[1].rsplit("@", 1)[0]


HA_TARGETS = {
    "username-with-extension": wire_user(name="Kitchen Panel", extension="101", sip_uri_user="kitchen-panel"),
    "extension": wire_user(name="Kitchen Panel", extension="101"),
    "username": wire_user(name="Kitchen Panel", sip_uri_user="kitchen-panel"),
    "text-name": wire_user(name="Kitchen Panel"),
}


def test_ha_serializes_each_addressing_layer():
    assert HA_TARGETS == {"username-with-extension": "kitchen-panel", "extension": "101",
                         "username": "kitchen-panel", "text-name": "Kitchen%20Panel"}


@pytest.fixture(scope="module")
def esp_registration_engine(tmp_path_factory):
    folder = tmp_path_factory.mktemp("esp-registration-engine")
    defines = folder / "esphome/core/defines.h"
    defines.parent.mkdir(parents=True)
    defines.write_text("")
    binary = folder / "registration"
    built = subprocess.run([
        "g++", "-std=c++17", "-g", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        "-DUSE_ESPHOME_VOIP_REGISTRATION", "-DUSE_VOIP_HA_AUTO_REGISTER", "-DVOIP_REGISTRATION_CAPACITY=2",
        "-I", str(folder), "-I", str(SOURCE), str(SOURCE / "sip_registration.cpp"),
        str(VOIP / "tests/fixtures/registration_engine.cpp"), "-lcrypto", "-o", str(binary),
    ], capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    return binary


@pytest.mark.parametrize("transport", ["udp", "tcp"])
@pytest.mark.parametrize("layer", sorted(HA_TARGETS))
def test_registered_esp_line_admits_every_ha_target(esp_registration_engine, transport, layer):
    result = subprocess.run([esp_registration_engine, "registrar-target", transport, HA_TARGETS[layer]],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"PASS registrar-target {transport}\n"
