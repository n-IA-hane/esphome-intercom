"""Move the actual codecs and RTP admission state between existing media owners."""

from dataclasses import dataclass


@dataclass(slots=True)
class RtpAudioHandoff:
    receiver: object
    encoder: object
    dtmf_decoder: object
    sender: object
    tx_converter: object
    remote_host: str
    remote_port: int
    received: object = None
    pending_packets: tuple = ()
    ready_tx: tuple = ()
    transferred: bool = False
    transport: object = None

    def retarget_receiver(self, output_pcm):
        """Move queued decoded frames before the receiver's partial native frame."""
        from .audio_ws_frames import DeferredAudioFrames

        ready, packets = [], []
        while self.received is not None and not self.received.empty():
            data, addr = self.received.get_nowait()
            if isinstance(data, DeferredAudioFrames):
                if data.receiver is self.receiver:
                    ready.extend(data.frames)
            else:
                packets.append((data, addr))
        self.pending_packets = tuple(packets)
        return self.receiver.retarget_output(output_pcm, pending_output=ready)

    def converter_for(self, input_pcm):
        """Retire a typed target fragment into the successor's input converter.

        Keep the effective payload mapping and native codec object. Retarget
        input to that codec's actual PCM, converting the retired WS fragment
        once instead of keeping the old browser PCM in the application's path.
        """
        from .core.audio_pcm import PcmFrameConverter

        previous_input = self.encoder.input_format
        pending = self.tx_converter.drain_pending_pcm()
        native = self.encoder.fmt.audio_format
        if previous_input != native:
            retiring = PcmFrameConverter(previous_input, native)
            ready = retiring.convert(pending, final_fragment=True) if pending else []
            self.ready_tx = tuple(ready)
            pending = retiring.drain_pending_pcm()
            self.encoder.retarget_input(native)
        converter = PcmFrameConverter(input_pcm, self.encoder.input_format)
        converter.prepend_pending_pcm(pending)
        return converter
