"""Bounded CDP observer for the existing browser PCM WebSocket protocol."""
import base64
import json
from pathlib import Path
import wave


class BrowserAudioCapture:
    def __init__(self, max_bytes=10 * 1024 * 1024, *, capture_pcm=True):
        self.max_bytes = max_bytes
        self.capture_pcm = capture_pcm
        self.total_bytes = 0
        self.formats = {}
        self.streams = {}
        self.errors = []

    def record(self, request_id, direction, frame, timestamp):
        """Observe wire geometry; retain PCM only when WAV capture is enabled."""
        try:
            if frame.get('opcode') == 1 and direction == 'rx':
                message = json.loads(frame.get('payloadData') or '{}')
                if not isinstance(message, dict):
                    raise ValueError('invalid browser audio configuration')
                formats = {}
                for side in ('tx', 'rx'):
                    if message.get(side + '_format'):
                        self.formats[(request_id, side)] = message[side + '_format']
                        formats[side + '_format'] = message[side + '_format']
                return {'kind': 'configuration', **formats} if formats else None
            if frame.get('opcode') != 2:
                return None
            data = base64.b64decode(frame['payloadData'], validate=True)
            if len(data) < 2 or data[0] != 1:
                raise ValueError('invalid browser audio frame')
            token = self.formats[(request_id, direction)]
            rate, pcm, channels, ptime = token.split(':')
            try:
                rate, channels, ptime = int(rate), int(channels), int(ptime)
            except (TypeError, ValueError):
                raise ValueError('invalid negotiated PCM dimensions') from None
            if not 8000 <= rate <= 192000 or not 1 <= channels <= 8 or ptime <= 0:
                raise ValueError('invalid negotiated PCM dimensions')
            width = {'s16le': 2, 's24le': 3, 's24le_in_s32': 4, 's32le': 4}.get(pcm)
            if width is None:
                raise ValueError('invalid negotiated PCM representation')
            payload = data[1:]
            if len(payload) % (channels * width):
                raise ValueError('unaligned browser PCM')
            samples = len(payload) // (channels * width)
            observed = {
                'kind': 'pcm', 'format': token, 'wire_bytes': len(data),
                'header_bytes': 1, 'payload_bytes': len(payload),
                'samples_per_channel': samples,
                'duration_ms': samples * 1000 / rate,
                'declared_frame_ms': ptime,
            }
            if not self.capture_pcm:
                return observed
            if pcm != 's16le':
                raise ValueError('WAV observer requires negotiated s16le')
            if self.total_bytes + len(payload) > self.max_bytes:
                raise ValueError('browser audio capture capacity exceeded')
            key = (request_id, direction, token)
            stream = self.streams.setdefault(key, {
                'rate': rate, 'channels': channels, 'pcm': bytearray(), 'events': [],
            })
            stream['pcm'].extend(payload)
            stream['events'].append({'timestamp': timestamp, 'bytes': len(payload)})
            self.total_bytes += len(payload)
            return observed
        except (ValueError, KeyError, TypeError) as exc:
            reason = str(exc)
            if reason not in self.errors:
                self.errors.append(reason)
            return None

    def save(self, output: Path):
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        report = {'errors': self.errors, 'streams': []}
        for index, ((_, direction, token), stream) in enumerate(self.streams.items(), 1):
            path = output / f'audio-{index:02d}-{direction}.wav'
            with wave.open(str(path), 'wb') as writer:
                writer.setframerate(stream['rate'])
                writer.setnchannels(stream['channels'])
                writer.setsampwidth(2)
                writer.writeframes(stream['pcm'])
            path.chmod(0o600)
            report['streams'].append({
                'file': path.name, 'direction': direction, 'format': token,
                'seconds': len(stream['pcm']) / (stream['rate'] * stream['channels'] * 2),
                'events': stream['events'],
            })
        report_path = output / 'capture.json'
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        report_path.chmod(0o600)
        return report
