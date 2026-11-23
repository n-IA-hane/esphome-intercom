"""Move the actual codecs and RTP admission state between existing media owners."""

from dataclasses import dataclass
from copy import deepcopy


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
        ready, commit = self.prepare_receiver(output_pcm)
        commit()
        return ready

    def prepare_receiver(self, output_pcm):
        """Prepare decoded queue conversion while the original owner stays live."""
        from .audio_ws_frames import DeferredAudioFrames

        # asyncio.Queue has no public peek API. This is the sole adapter to its
        # FIFO, read synchronously with no yield before the prepared commit.
        queued = tuple(self.received._queue) if self.received is not None else ()
        ready, packets = [], list(self.pending_packets)
        for data, addr in queued:
            if isinstance(data, DeferredAudioFrames):
                if data.receiver is self.receiver:
                    ready.extend(data.frames)
            else:
                packets.append((data, addr))
        ready, commit_receiver = self.receiver.prepare_retarget_output(output_pcm, pending_output=ready)
        def commit():
            commit_receiver()
            for _item in queued:
                self.received.get_nowait()
            self.pending_packets = tuple(packets)
        return ready, commit

    def converter_for(self, input_pcm):
        """Retire a typed target fragment into the successor's input converter.

        Keep the effective payload mapping and native codec object. Retarget
        input to that codec's actual PCM, converting the retired WS fragment
        once instead of keeping the old browser PCM in the application's path.
        """
        converter, _ready, commit = self.prepare_converter_for(input_pcm)
        commit()
        return converter

    def prepare_converter_for(self, input_pcm):
        """Prepare TX retirement without consuming browser or encoder samples."""
        from .core.audio_pcm import PcmFrameConverter

        previous_input = self.encoder.input_format
        pending = deepcopy(self.tx_converter).drain_pending_pcm()
        native = self.encoder.fmt.audio_format
        ready = []
        if previous_input != native:
            retiring = PcmFrameConverter(previous_input, native)
            ready = retiring.convert(pending, final_fragment=True) if pending else []
            pending = retiring.drain_pending_pcm()
        commit_encoder = self.encoder.prepare_retarget_input(native)
        converter = PcmFrameConverter(input_pcm, native)
        converter.prepend_pending_pcm(pending)
        def commit():
            self.tx_converter.drain_pending_pcm()
            commit_encoder()
            self.ready_tx = tuple(ready)
        return converter, ready, commit
