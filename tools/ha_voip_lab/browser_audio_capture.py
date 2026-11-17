"""Bounded CDP observer for the existing browser PCM WebSocket protocol."""
import base64
import json
from pathlib import Path
import wave


class BrowserAudioCapture:
    def __init__(self, max_bytes=10 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.total_bytes = 0
        self.formats = {}
        self.streams = {}
        self.errors = []

    def record(self, request_id, direction, frame, timestamp):
        try:
            if frame.get('opcode') == 1 and direction == 'rx':
                message = json.loads(frame.get('payloadData') or '{}')
                for side in ('tx', 'rx'):
                    if message.get(side + '_format'):
                        self.formats[(request_id, side)] = message[side + '_format']
                return
            if frame.get('opcode') != 2:
                return
            data = base64.b64decode(frame['payloadData'], validate=True)
            if len(data) < 2 or data[0] != 1:
                raise ValueError('invalid browser audio frame')
            token = self.formats[(request_id, direction)]
            rate, pcm, channels, _ptime = token.split(':')
            if pcm != 's16le':
                raise ValueError('WAV observer requires negotiated s16le')
            rate, channels = int(rate), int(channels)
            if not 8000 <= rate <= 192000 or not 1 <= channels <= 8:
                raise ValueError('invalid negotiated PCM dimensions')
            payload = data[1:]
            if len(payload) % (channels * 2):
                raise ValueError('unaligned browser PCM')
            if self.total_bytes + len(payload) > self.max_bytes:
                raise ValueError('browser audio capture capacity exceeded')
            key = (request_id, direction, token)
            stream = self.streams.setdefault(key, {
                'rate': rate, 'channels': channels, 'pcm': bytearray(), 'events': [],
            })
            stream['pcm'].extend(payload)
            stream['events'].append({'timestamp': timestamp, 'bytes': len(payload)})
            self.total_bytes += len(payload)
        except (ValueError, KeyError, TypeError) as exc:
            reason = str(exc)
            if reason not in self.errors:
                self.errors.append(reason)

    def save(self, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        report = {'errors': self.errors, 'streams': []}
        for index, ((_, direction, token), stream) in enumerate(self.streams.items(), 1):
            path = output / f'audio-{index:02d}-{direction}.wav'
            with wave.open(str(path), 'wb') as writer:
                writer.setframerate(stream['rate'])
                writer.setnchannels(stream['channels'])
                writer.setsampwidth(2)
                writer.writeframes(stream['pcm'])
            report['streams'].append({
                'file': path.name, 'direction': direction, 'format': token,
                'seconds': len(stream['pcm']) / (stream['rate'] * stream['channels'] * 2),
                'events': stream['events'],
            })
        (output / 'capture.json').write_text(json.dumps(report, indent=2) + '\n')
        return report
