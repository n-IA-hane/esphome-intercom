"""One SSH-owned tcpdump writer, verified PID, bounded SIGINT and capinfos.

No process starts on import. The caller owns when capture is authorized.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shlex


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def capture_filter(expression):
    require(bool(expression.strip()) and '\0' not in expression, 'A concrete BPF filter is required')
    # Exclude the capture SSH stream and credential-bearing HA/ESP API traffic.
    excluded = '22 or 80 or 443 or 6053 or 8123 or 18123'
    return f'({expression}) and not (port {excluded.replace(" or ", " or port ")})'


def ssh_command(host, prefix, argv):
    require(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]*', host) is not None,
            'SSH host must be an existing trusted host/alias')
    require(all(isinstance(arg, str) and '\0' not in arg for arg in [*prefix, *argv]),
            'Invalid remote argument')
    return ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=10', host, shlex.join([*prefix, *argv])]


IDENTITY = '''import json,os,pathlib,sys
p=int(sys.argv[1]);base=pathlib.Path('/proc')/str(p)
st=(base/'stat').read_text().rsplit(')',1)[1].split()
print(json.dumps({'pid':p,'starttime':st[19],
 'cmdline':(base/'cmdline').read_bytes().rstrip(bytes([0])).decode().split(chr(0)),
 'stdout':os.readlink(base/'fd/1')}))
'''

STOP = '''import json,os,pathlib,signal,sys
expected=json.loads(sys.argv[1]);p=expected['pid'];fd=os.pidfd_open(p)
try:
 base=pathlib.Path('/proc')/str(p)
 st=(base/'stat').read_text().rsplit(')',1)[1].split()
 actual={'pid':p,'starttime':st[19],
  'cmdline':(base/'cmdline').read_bytes().rstrip(bytes([0])).decode().split(chr(0)),
  'stdout':os.readlink(base/'fd/1')}
 if actual!=expected:raise RuntimeError('Owned capture process identity changed; no signal sent')
 signal.pidfd_send_signal(fd,signal.SIGINT)
 print(json.dumps({'signalled_owned_pid':p,'signal':'SIGINT'}))
finally:os.close(fd)
'''


def tcpdump_statistics(lines):
    """Preserve reported OS counters, never infer a missing counter as zero."""
    labels = {"captured": "captured", "received by filter": "received_by_filter",
              "dropped by kernel": "dropped_by_kernel"}
    values = {key: [] for key in labels.values()}
    for line in lines:
        found = re.fullmatch(r"(\d+) packets? (captured|received by filter|dropped by kernel)", line.strip())
        if found:
            values[labels[found[2]]].append(int(found[1]))
    counts = {key: items[0] if len(items) == 1 else None for key, items in values.items()}
    issues = [("missing " if not items else "duplicate ") + key + " counter"
              for key, items in values.items() if len(items) != 1]
    quality = ("unknown" if issues else "kernel_drops_reported" if counts["dropped_by_kernel"]
               else "zero_kernel_drops_reported")
    # received-by-filter semantics differ by OS; do not require arithmetic equality.
    return counts, quality, issues


class SshPcapWriter:
    def __init__(self, *, host, remote_prefix=(), interface='any', bpf, output,
                 require_zero_kernel_drops=False):
        require(re.fullmatch(r'[A-Za-z0-9_.:-]+', interface) is not None, 'Invalid capture interface')
        self.host, self.prefix = host, list(remote_prefix)
        self.output = Path(output)
        self.argv = ['tcpdump', '-n', '-i', interface,
                     '-U', '-s', '0', '-w', '-', capture_filter(bpf)]
        self.identity = None
        self.process = None
        self.reader = None
        self.stream = None
        self.stderr = []
        self.require_zero_kernel_drops = require_zero_kernel_drops
        self.ready = asyncio.Event()
        self.closed = False
        self.receipt = {'status': 'not_started', 'host': host, 'remote_prefix': self.prefix,
                        'argv': self.argv, 'pcap': str(self.output),
                        'physical_qualified': False, 'acoustic_qualified': False,
                        'require_zero_kernel_drops': require_zero_kernel_drops}

    async def remote(self, argv, timeout=15):
        process = await asyncio.create_subprocess_exec(*ssh_command(self.host, self.prefix, argv),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        require(process.returncode == 0, 'SSH capture control failed: ' + stderr.decode(errors='replace')[-600:])
        return stdout

    async def read_stderr(self):
        assert self.process.stderr is not None
        async for raw in self.process.stderr:
            line = raw.decode(errors='replace').rstrip()
            self.stderr.append(line)
            found = re.fullmatch(r'QUAL_CAPTURE_PID=(\d+)', line)
            if found:
                self.receipt['remote_pid_reported'] = int(found[1])
            if 'listening on ' in line and self.receipt.get('remote_pid_reported'):
                self.ready.set()

    async def start(self):
        require(self.process is None and not self.closed, 'Capture writer cannot be reused')
        self.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stream = os.fdopen(os.open(self.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb')
        bootstrap = ('import os,sys\nos.environ["LC_ALL"]="C"\n'
                     'sys.stderr.write("QUAL_CAPTURE_PID=%d\\n"%os.getpid());sys.stderr.flush()\n'
                     'os.execvp("tcpdump",' + repr(self.argv) + ')\n')
        command = ssh_command(self.host, self.prefix, ['python3', '-c', bootstrap])
        self.receipt.update(status='starting', command=command)
        try:
            self.process = await asyncio.create_subprocess_exec(*command,
                stdin=asyncio.subprocess.DEVNULL, stdout=self.stream, stderr=asyncio.subprocess.PIPE)
            self.receipt['local_ssh_pid'] = self.process.pid
            self.reader = asyncio.create_task(self.read_stderr())
            await asyncio.wait_for(self.ready.wait(), 15)
            require(self.process.returncode is None, 'tcpdump exited before capture readiness')
            self.identity = json.loads(await self.remote(
                ['python3', '-c', IDENTITY, str(self.receipt['remote_pid_reported'])]))
            require(self.identity['cmdline'] == self.argv and self.identity['stdout'].startswith('pipe:'),
                    'Remote PID is not the exact stdout PCAP writer')
            self.receipt.update(status='capturing', verified_remote_identity=self.identity)
            self.save()
            return self
        except BaseException as error:
            self.receipt.update(status='capture_start_failed', error=type(error).__name__ + ': ' + str(error))
            self.save()
            # Preserve unknown remote owners instead of signalling an unverified PID.
            if self.process is not None and self.process.returncode is None and self.identity is not None:
                await self.stop()
            elif self.stream is not None:
                self.stream.close()
            raise

    def save(self):
        path = self.output.with_suffix(self.output.suffix + '.writer.json')
        path.write_text(json.dumps({**self.receipt, 'stderr': self.stderr}, indent=2) + '\n')
        path.chmod(0o600)

    async def stop(self):
        if self.closed:
            return self.receipt
        self.closed = True
        quality_error = None
        try:
            require(self.identity is not None, 'No verified remote capture identity; manual cleanup required')
            signalled = json.loads(await self.remote(['python3', '-c', STOP, json.dumps(self.identity)]))
            require(signalled.get('signalled_owned_pid') == self.identity['pid'], 'Capture stop PID mismatch')
            await asyncio.wait_for(self.process.wait(), 20)
            if self.reader is not None:
                await self.reader
            self.stream.close()
            self.receipt['ssh_exit_code'] = self.process.returncode
            counts, quality, issues = tcpdump_statistics(self.stderr)
            self.receipt.update(tcpdump_counts=counts, capture_quality=quality,
                                capture_quality_issues=issues)
            require(self.process.returncode in {0, 130, -2}, 'tcpdump/SSH stopped with an unexpected error')
            validator = await asyncio.create_subprocess_exec('capinfos', '-c', '-s', '-u', str(self.output),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(validator.communicate(), 20)
            require(validator.returncode == 0, 'capinfos rejected the actual capture: ' + stderr.decode(errors='replace'))
            info = self.output.with_suffix(self.output.suffix + '.capinfos.txt')
            info.write_bytes(stdout)
            info.chmod(0o600)
            with self.output.open('rb') as data:
                digest = hashlib.file_digest(data, 'sha256').hexdigest()
            self.receipt.update(status='capture_closed_capinfos_accepted', pcap_sha256=digest,
                                bytes=self.output.stat().st_size, capinfos=str(info),
                                media_qualified=False, capture_file_valid=True)
            if self.require_zero_kernel_drops and quality != "zero_kernel_drops_reported":
                quality_error = ("Capture quality requirement failed: " + quality +
                                 "; dropped_by_kernel=" + str(counts["dropped_by_kernel"]))
                self.receipt.update(status="capture_quality_failed", error=quality_error)
        except BaseException as error:
            self.receipt.update(status='capture_cleanup_failed', error=type(error).__name__ + ': ' + str(error))
            raise
        finally:
            if self.stream is not None and not self.stream.closed:
                self.stream.close()
            self.save()
        if quality_error:
            raise RuntimeError(quality_error)
        return self.receipt
