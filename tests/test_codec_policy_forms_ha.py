"""Native codec editors retain order, disabled rows and separate inheritance."""

from unittest.mock import Mock

import pytest
from homeassistant.helpers.selector import ObjectSelector
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.config_flow import (
    PhoneSubentryFlowHandler, TrunkFormMixin, VoipStackConfigFlow, _disabled_trunk_data,
)
from custom_components.voip_stack.phone_endpoint import EndpointKind

pytestmark = pytest.mark.ha


class TrunkEditor(TrunkFormMixin, VoipStackConfigFlow):
    """Use the production form with storage isolated from SIP registration."""


def selector(form, name):
    return next(value for key, value in form["data_schema"].schema.items() if key.schema == name)


async def test_global_codec_form_uses_native_ordered_rows(hass):
    flow = VoipStackConfigFlow()
    flow.hass = hass
    flow._current_entry_data = lambda: (None, {})
    form = await flow.async_step_user()
    for name in ("audio_codecs", "video_codecs"):
        field = selector(form, name)
        assert isinstance(field, ObjectSelector)
        assert field.config["multiple"] and field.config["label_field"] == "codec"
        assert set(field.config["fields"]) == {"codec", "enabled"}
        rows = [{"codec": "PCMU" if name == "audio_codecs" else "JPEG", "enabled": False}]
        assert field(rows) == rows


@pytest.mark.parametrize("audio", [[], [{"codec": "PCMU", "enabled": False}]])
async def test_global_form_rejects_audio_without_an_enabled_codec(hass, audio):
    flow = VoipStackConfigFlow()
    flow.hass = hass
    flow._current_entry_data = lambda: (None, {})
    form = await flow.async_step_user()
    values = form["data_schema"]({"audio_codecs": audio})
    result = await flow.async_step_user(values)
    assert result["type"] == "form" and result["errors"] == {"audio_codecs": "codecs_invalid"}


async def test_new_trunk_does_not_persist_suggested_inherited_rows(hass):
    flow = TrunkEditor()
    flow.hass = hass
    entry = MockConfigEntry(domain="voip_stack", data={"audio_codecs": ["PCMU"]})
    entry.add_to_hass(hass)
    flow._current_entry_data = lambda: (entry, {"trunk_register": False})
    flow._store_entry = Mock(side_effect=lambda data: data)
    form = await flow.async_step_trunk()
    values = form["data_schema"]({"trunk_server": "192.0.2.1", "trunk_allowed_ips": ["192.0.2.2"],
        "trunk_inbound_default_target": "Desk"})
    stored = await flow.async_step_trunk(values)
    assert "trunk_audio_codecs" not in stored and "trunk_video_codecs" not in stored


def test_normalized_trunk_preserves_inheritance_and_disabled_rows():
    assert "trunk_audio_codecs" not in normalized_trunk_config({})
    assert "trunk_video_codecs" not in normalized_trunk_config({})
    rows = [{"codec": "PCMU", "enabled": True}, {"codec": "PCMA", "enabled": False}]
    stored = normalized_trunk_config({"trunk_audio_codecs": rows, "trunk_video_codecs": []})
    assert stored["trunk_audio_codecs"] == rows and stored["trunk_video_codecs"] == []
    disabled = _disabled_trunk_data({}, stored)
    assert disabled["trunk_audio_codecs"] == rows and disabled["trunk_video_codecs"] == []


def test_trunk_client_resolves_global_policy_without_persisting_the_result():
    from custom_components.voip_stack.core.codec_policy import CodecPolicy
    from custom_components.voip_stack.trunk_runtime import trunk_client_config

    parent = CodecPolicy.from_config({"audio_codecs": ["PCMU", "PCMA"], "video_codecs": []}, prefix="")
    raw = normalized_trunk_config({})
    client = trunk_client_config(raw, parent=parent)
    assert client.audio_codecs == ("PCMU", "PCMA") and client.video_codecs == ()
    assert client.audio_ordered and client.video_ordered
    assert "trunk_audio_codecs" not in raw and "trunk_video_codecs" not in raw
    legacy_default = trunk_client_config(raw)
    assert not legacy_default.audio_ordered and not legacy_default.video_ordered


async def test_trunk_form_retains_rows_and_inherits_each_medium_separately(hass):
    previous = {"trunk_register": False, "trunk_video_codecs": ["JPEG"],
                "trunk_audio_codecs": ["PCMA", "PCMU"]}
    flow = TrunkEditor()
    flow.hass = hass
    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    flow._current_entry_data = lambda: (entry, previous)
    flow._store_entry = Mock(side_effect=lambda data: data)
    form = await flow.async_step_trunk()
    rows = [{"codec": "PCMU", "enabled": True}, {"codec": "PCMA", "enabled": False}]
    values = form["data_schema"]({"trunk_server": "192.0.2.1", "trunk_allowed_ips": ["192.0.2.2"],
        "trunk_inbound_default_target": "Desk", "trunk_audio_codecs": rows,
        "trunk_audio_codecs_inherit": False, "trunk_video_codecs_inherit": True})
    stored = await flow.async_step_trunk(values)
    assert stored["trunk_audio_codecs"] == rows
    assert "trunk_video_codecs" not in stored
    assert not any(key.endswith("_inherit") for key in stored)


async def test_trunk_form_accepts_saved_legacy_lists_without_new_controls(hass):
    flow = TrunkEditor()
    flow.hass = hass
    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    flow._current_entry_data = lambda: (entry, {"trunk_register": False})
    flow._store_entry = Mock(side_effect=lambda data: data)
    form = await flow.async_step_trunk()
    values = form["data_schema"]({"trunk_server": "192.0.2.1", "trunk_allowed_ips": ["192.0.2.2"],
        "trunk_inbound_default_target": "Desk", "trunk_audio_codecs": ["PCMU"], "trunk_video_codecs": []})
    stored = await flow.async_step_trunk(values)
    assert stored["trunk_audio_codecs"] == [{"codec": "PCMU", "enabled": True}]
    assert stored["trunk_video_codecs"] == []


def test_account_form_preserves_explicit_defaults_and_empty_video(hass):
    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    flow = PhoneSubentryFlowHandler()
    flow.hass = hass
    flow._get_entry = lambda: entry
    flow._validate_aliases = lambda **kwargs: True
    current = {"endpoint_id": "sip:desk", "name": "Desk", "username": "desk", "password": "fixture-only",
               "audio_codecs": ["PCMA", "PCMU"], "video_codecs": ["JPEG"]}
    flow._current_data = lambda: current
    rows = [{"codec": "PCMU", "enabled": True}, {"codec": "PCMA", "enabled": False}]
    stored, errors = flow._normalized_common({"audio_codecs": rows, "audio_codecs_inherit": False,
        "video_codecs": [], "video_codecs_inherit": False}, kind=EndpointKind.SIP_ACCOUNT)
    assert not errors and stored["audio_codecs"] == rows and stored["video_codecs"] == []
    inherited, errors = flow._normalized_common({"audio_codecs_inherit": True,
        "video_codecs": [], "video_codecs_inherit": False}, kind=EndpointKind.SIP_ACCOUNT)
    assert not errors and "audio_codecs" not in inherited and inherited["video_codecs"] == []
    assert not any(key.endswith("_inherit") for key in inherited)
