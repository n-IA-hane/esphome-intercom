"""VoIP responses through HA sentence recognition and template rendering."""

from pathlib import Path
from unittest.mock import AsyncMock

from homeassistant.core import Context
from homeassistant.setup import async_setup_component
from homeassistant.components.conversation.agent_manager import async_converse
import pytest
import yaml

from custom_components.voip_stack import assist_intents

pytestmark = pytest.mark.ha
EXAMPLES = Path(__file__).parents[1] / "examples/home-assistant/custom_sentences"


async def prepare(hass, monkeypatch, language, *, legacy=False, silent=False):
    data = yaml.safe_load((EXAMPLES / language / "voip_stack.yaml").read_text())
    if legacy:
        data.pop("responses")
        for definition in data["intents"].values():
            for group in definition["data"]:
                group.pop("slots")
                group.pop("response")
        data["intents"]["VoipCall"]["data"][0]["sentences"] = ["telephone {target} now"]
    if silent:
        data["responses"]["intents"]["VoipCall"]["default"] = "{{ '' }}"
    path = Path(hass.config.path("custom_sentences", language, "voip_stack.yaml"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    assist_intents.async_register_assist_intents(hass)
    monkeypatch.setattr(assist_intents, "_origin_device", AsyncMock(return_value={"device_id": "desk", "name": "Desk"}))
    resolve = AsyncMock(return_value=assist_intents.ContactResolution(canonical="Kitchen phone"))
    monkeypatch.setattr(assist_intents, "_resolve_contact_or_area", resolve)
    records = []

    async def action(call):
        records.append(call)

    for service in ("call", "hangup", "answer", "decline"):
        hass.services.async_register("voip_stack", service, action)
    return resolve, records


@pytest.mark.parametrize("language,text,expected", [
    ("en", "call kitchen", "Calling Kitchen phone."),
    ("it", "chiama cucina", "Chiamo Kitchen phone."),
    ("de", "rufe küche an", "Ich rufe Kitchen phone an."),
])
async def test_localized_success_uses_resolved_contact(hass, monkeypatch, language, text, expected):
    _resolve, records = await prepare(hass, monkeypatch, language)
    result = await async_converse(hass, text, None, Context(), language=language)
    assert result.response.speech["plain"]["speech"] == expected
    assert records[0].data["destination"] == "Kitchen phone"
    assert result.response.as_dict()["response_type"] == "action_done"


@pytest.mark.parametrize("language,text,fragment", [
    ("en", "call nowhere", "nowhere"),
    ("it", "chiama nessuno", "nessuno"),
    ("de", "rufe niemand an", "niemand"),
])
async def test_localized_error_keeps_target_and_error_type(hass, monkeypatch, language, text, fragment):
    resolve, records = await prepare(hass, monkeypatch, language)
    resolve.return_value = assist_intents.ContactResolution(error="not_found")
    result = await async_converse(hass, text, None, Context(), language=language)
    assert fragment in result.response.speech["plain"]["speech"]
    assert result.response.as_dict()["response_type"] == "error"
    assert result.response.as_dict()["data"]["code"] == "failed_to_handle"
    assert records == []


async def test_old_custom_sentences_keep_existing_fallback(hass, monkeypatch):
    await prepare(hass, monkeypatch, "en", legacy=True)
    result = await async_converse(hass, "telephone kitchen now", None, Context(), language="en")
    assert result.response.speech["plain"]["speech"] == "Calling Kitchen phone."


async def test_explicit_empty_template_is_silent_without_cancelling_action(hass, monkeypatch):
    _resolve, records = await prepare(hass, monkeypatch, "en", silent=True)
    result = await async_converse(hass, "call kitchen", None, Context(), language="en")
    assert result.response.speech["plain"]["speech"] == ""
    assert len(records) == 1


@pytest.mark.parametrize("language,sentences,expected", [
    ("en", ("hang up", "answer", "decline"), ("OK.", "Answering.", "Declining.")),
    ("it", ("riaggancia", "rispondi", "rifiuta"), ("Va bene.", "Rispondo.", "Rifiuto la chiamata.")),
    ("de", ("auflegen", "annehmen", "ablehnen"), ("OK.", "Ich nehme den Anruf an.", "Ich lehne den Anruf ab.")),
])
async def test_localized_call_controls(hass, monkeypatch, language, sentences, expected):
    _resolve, records = await prepare(hass, monkeypatch, language)
    for text, speech in zip(sentences, expected, strict=True):
        result = await async_converse(hass, text, None, Context(), language=language)
        assert result.response.speech["plain"]["speech"] == speech
    assert [call.service for call in records] == ["hangup", "answer", "decline"]


@pytest.mark.parametrize("error", ["missing", "ambiguous", "ambiguous_area", "area_empty", "ambiguous_area_device"])
async def test_resolution_failures_render_without_ha_generic_error(hass, monkeypatch, error):
    resolve, records = await prepare(hass, monkeypatch, "en")
    resolve.return_value = assist_intents.ContactResolution(error=error)
    result = await async_converse(hass, "call kitchen", None, Context(), language="en")
    speech = result.response.speech["plain"]["speech"]
    assert speech
    assert "couldn't understand" not in speech
    assert result.response.as_dict()["response_type"] == "error"
    assert records == []


async def test_unknown_origin_is_localized_error(hass, monkeypatch):
    _resolve, records = await prepare(hass, monkeypatch, "it")
    monkeypatch.setattr(assist_intents, "_origin_device", AsyncMock(return_value=None))
    result = await async_converse(hass, "chiama cucina", None, Context(), language="it")
    assert result.response.speech["plain"]["speech"] == "Non so quale dispositivo VoIP ha ricevuto il comando."
    assert result.response.as_dict()["response_type"] == "error"
    assert records == []


@pytest.mark.parametrize("text,service,expected", [
    ("call kitchen", "call", "I could not call Kitchen phone."),
    ("hang up", "hangup", "I could not hang up the VoIP call."),
    ("answer", "answer", "I could not answer the VoIP call."),
    ("decline", "decline", "I could not decline the VoIP call."),
])
async def test_service_errors_render_action_specific_response(hass, monkeypatch, text, service, expected):
    await prepare(hass, monkeypatch, "en")

    async def failed(_call):
        raise RuntimeError("Details belong in the diagnostic log")

    hass.services.async_register("voip_stack", service, failed)
    result = await async_converse(hass, text, None, Context(), language="en")
    assert result.response.speech["plain"]["speech"] == expected
    assert result.response.as_dict()["response_type"] == "error"
