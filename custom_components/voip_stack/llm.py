"""Expose the existing satellite call intents to Home Assistant's Assist LLM API."""

from homeassistant.components.llm import LLMTools
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import intent
from homeassistant.helpers.llm import LLM_API_ASSIST, IntentTool, LLMContext

from .assist_intents import INTENT_TYPES


@callback
def async_get_tools(
    hass: HomeAssistant, llm_context: LLMContext, api_id: str
) -> LLMTools | None:
    """Offer registered call handlers when the request identifies its satellite."""
    if api_id != LLM_API_ASSIST or not llm_context.device_id:
        return None

    tools = [
        IntentTool(handler.intent_type, handler)
        for handler in intent.async_get(hass)
        if handler.intent_type in INTENT_TYPES
    ]
    if not tools:
        return None

    return LLMTools(
        tools=tools,
        prompt=(
            "Use VoipCall to make a real phone call from the voice satellite "
            "that heard the request. Pass the requested contact, area or phone "
            "number as target. Use the VoIP intent tools for answering, declining "
            "or hanging up calls; do not simulate a call or use an announcement."
        ),
    )
