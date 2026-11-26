"""Optional processing of saved messages through native Home Assistant APIs."""

import asyncio
from contextlib import aclosing
from contextvars import ContextVar

from homeassistant.exceptions import ServiceValidationError

from .core.audio_format import AudioFormat
from .core.audio_pcm import PcmFrameConverter
from .core.media_archive import ArchiveError
from .duration_policy import resolve_service_timeout
from .mailbox import get_archive, selected_mailbox, message_audio_reference

current_message: ContextVar[tuple[str, str] | None] = ContextVar("voip_voicemail_message", default=None)


async def selected_message(call):
    """Select the trigger's immutable message, or an explicitly selected one."""
    bound = current_message.get()
    mailbox = call.data.get("mailbox")
    identity = call.data.get("message_id")
    if mailbox or identity:
        if not mailbox or not identity:
            raise ServiceValidationError("Choose both a mailbox and a message ID")
    elif bound:
        mailbox, identity = bound
    else:
        raise ServiceValidationError("Use a voicemail-received trigger or choose a mailbox and message ID")
    endpoint = await selected_mailbox(call, mailbox)
    archive = await get_archive(call.hass)
    try:
        message = archive.message(identity, endpoint.endpoint_id)
    except ArchiveError as err:
        raise ServiceValidationError(str(err)) from err
    if message["kind"] != "voicemail":
        raise ServiceValidationError("Select a voicemail message")
    return archive, message


async def transcription_pcm(archive, message):
    """Adapt a saved message for STT; its original recording stays unchanged."""
    destination = AudioFormat(16000, "s16le", 1, 20)
    for index, segment in enumerate(message["segments"]):
        source = AudioFormat(segment["sample_rate"], {2: "s16le", 3: "s24le", 4: "s32le"}[segment["sample_width"]],
                             segment["channels"], 20)
        converter = PcmFrameConverter(source, destination)
        async with aclosing(archive.pcm_frames(message["id"], message["mailbox_id"], index)) as frames:
            async for pcm in frames:
                # A final partial frame is padded only in the STT stream.
                if len(pcm) < source.nominal_frame_bytes:
                    pcm += bytes(source.nominal_frame_bytes - len(pcm))
                for converted in converter.convert(pcm):
                    yield converted


async def async_transcribe_voicemail(call):
    from homeassistant.components import stt

    timeout = resolve_service_timeout(call.hass, "transcription_timeout", call.data.get("timeout"))
    archive, message = await selected_message(call)
    engine = (stt.async_get_speech_to_text_entity(call.hass, call.data["stt_entity_id"])
              if stt.DATA_COMPONENT in call.hass.data else None)
    if engine is None or not engine.available:
        raise ServiceValidationError("The selected speech-to-text entity is unavailable")
    metadata = stt.SpeechMetadata(
        language=call.data["language"], format=stt.AudioFormats.WAV, codec=stt.AudioCodecs.PCM,
        bit_rate=stt.AudioBitRates.BITRATE_16, sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
        channel=stt.AudioChannels.CHANNEL_MONO,
    )
    if not engine.check_metadata(metadata):
        raise ServiceValidationError("The speech-to-text entity must support the selected language and 16 kHz mono PCM")
    try:
        async with asyncio.timeout(timeout):
            async with aclosing(transcription_pcm(archive, message)) as frames:
                result = await engine.internal_async_process_audio_stream(metadata, frames)
    except TimeoutError as err:
        raise ServiceValidationError("Voicemail transcription timed out") from err
    except (ArchiveError, OSError) as err:
        raise ServiceValidationError("The recorded message is no longer available") from err
    if result.result != stt.SpeechResultState.SUCCESS:
        raise ServiceValidationError("The speech-to-text provider could not transcribe this message")
    return {"text": result.text or "", "language": metadata.language,
            "mailbox_id": message["mailbox_id"], "message_id": message["id"], "caller": message["caller"]}


async def async_get_voicemail(call):
    """Return the saved message's existing file without making a copy."""
    archive, message = await selected_message(call)
    return {**message_audio_reference(call.hass, archive, message),
            "mailbox_id": message["mailbox_id"], "message_id": message["id"], "caller": message["caller"]}
