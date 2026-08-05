"""Constants for VoIP Stack integration."""

import json
from pathlib import Path

DOMAIN = "voip_stack"
SIP_CALL_ENDED_EVENT = "voip_stack.call_ended"
CONF_ASSIST_INTENTS = "assist_intents"
CONF_ASSIST_ENDPOINT_ENABLED = "assist_endpoint_enabled"
CONF_ASSIST_EXTENSION = "assist_extension"
CONF_ASSIST_PIPELINE = "assist_pipeline"
CONF_ASSIST_ADVANCED_CALL_CONTEXT = "assist_advanced_call_context"
CONF_DEBUG_MODE = "debug_mode"
CONF_MEDIA_CAPTURE = "media_capture"
CONF_PREFERRED_PHONE_DEVICE_ID = "preferred_phone_device_id"
CONF_INITIAL_PHONE_CREATED = "initial_phone_created"
# Keep the persisted key stable for configured entries created before the SIP
# video profile graduated from preview status.
CONF_SIP_VIDEO = "experimental_sip_video"
CONF_VIDEO_TRANSCODING = "video_transcoding_enabled"
CONF_VIDEO_CAMERA_SEND = "video_camera_send_enabled"
CONF_HA_SOFTPHONE_DND = "ha_softphone_dnd"
CONF_HA_SOFTPHONE_EXTENSION = "ha_softphone_extension"
CONF_HA_SOFTPHONE_RING_GROUP = "ha_softphone_ring_group"
CONF_HA_SOFTPHONE_CONFERENCE_GROUP = "ha_softphone_conference_group"
CONF_HA_SOFTPHONE_CONFERENCE_RING = "ha_softphone_conference_ring"
CONF_REGISTRAR_ENABLED = "sip_registrar_enabled"
CONF_SIP_ACCOUNTS = "sip_accounts"
CONF_PHONEBOOK_CONTACTS = "phonebook_contacts"
CONF_TRUNK_ENABLED = "trunk_enabled"
CONF_TRUNK_TRANSPORT = "trunk_transport"
CONF_TRUNK_SERVER = "trunk_server"
CONF_TRUNK_PORT = "trunk_port"
CONF_TRUNK_DOMAIN = "trunk_domain"
CONF_TRUNK_USERNAME = "trunk_username"
CONF_TRUNK_AUTH_USERNAME = "trunk_auth_username"
CONF_TRUNK_PASSWORD = "trunk_password"
CONF_TRUNK_EXPIRES = "trunk_register_expires"
CONF_TRUNK_OUTBOUND_PROXY = "trunk_outbound_proxy"
CONF_TRUNK_INBOUND_DEFAULT_TARGET = "trunk_inbound_default_target"
CONF_TRUNK_INBOUND_MODE = "trunk_inbound_mode"
CONF_AUTOMATION_ROUTING_ENABLED = "automation_routing_enabled"
CONF_TRUNK_DTMF_ENABLED = "trunk_dtmf_enabled"
CONF_TRUNK_DTMF_TIMEOUT_MS = "trunk_dtmf_timeout_ms"
CONF_TRUNK_DTMF_TERMINATOR = "trunk_dtmf_terminator"
TRUNK_INBOUND_MODE_DIRECT = "direct"
TRUNK_INBOUND_MODE_DTMF = "dtmf"

# Version from manifest.json
_MANIFEST = Path(__file__).parent / "manifest.json"
with open(_MANIFEST, encoding="utf-8") as _f:
    INTEGRATION_VERSION = json.load(_f).get("version", "0.0.0")

# Frontend URL base for serving the Lovelace card
URL_BASE = "/voip-stack"
HA_PEER_FALLBACK_NAME = "voip-stack"
HA_SOFTPHONE_DEVICE_ID = "__voip_stack_ha_softphone__"
HA_SOFTPHONE_ENDPOINT_ENTITY_ID = "sensor.voip_stack_ha_softphone_voip_endpoint"
HA_SOFTPHONE_CALL_STATE_ENTITY_ID = "sensor.voip_stack_call_state"

VOIP_STACK_SIP_PORT = 5060
VOIP_STACK_RTP_PORT = 40000
