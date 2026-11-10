"""Real local PTY pressure on the console owner; never starts a SIP peer."""
import array
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import termios
import time
import pytest

ROOT=Path(__file__).resolve().parents[1]
source=Path(os.environ.get('VOIP_CONSOLE_OWNER_SOURCE',ROOT/'tools/ha_softphone_matrix.py'))
spec=importlib.util.spec_from_file_location('matrix_console_owner',source)
matrix=importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)

CHILD=r'''
import os,pathlib,sys,time
root=pathlib.Path(sys.argv[1])
print('baresip is ready.',flush=True) # Only constructor readiness, no SIP registration.
while not (root/'start-output').exists():time.sleep(.005)
(root/'output-started').touch()
for i in range(2048):os.write(2,b'console-pressure:'+b'x'*4080+b'\n')
(root/'output-finished').touch()
for line in sys.stdin:
 if line.strip()=='/hangup':(root/'hangup-processed').touch()
 if line.strip()=='/quit':break
'''


def until(predicate,timeout):
    deadline=time.monotonic()+timeout
    while not predicate() and time.monotonic()<deadline:
        time.sleep(.01)
    return predicate()


def test_hangup_progresses_under_real_pty_output_pressure(monkeypatch,tmp_path):
    real_popen=subprocess.Popen
    def console_child(argv,**kwargs):
        assert argv[:2]==['baresip','-f']
        return real_popen([sys.executable,'-u','-c',CHILD,str(tmp_path)],**kwargs)
    monkeypatch.setattr(matrix.subprocess,'Popen',console_child)
    monkeypatch.setattr(matrix,'TEST_CAPTURE_DIR',tmp_path)
    monkeypatch.delenv('VOIP_TEST_BARESIP_MODULE_PATH',raising=False)
    owner=matrix.BareSip(tmp_path,wait_for_registration=False)
    try:
        (tmp_path/'start-output').touch()
        assert until((tmp_path/'output-started').exists,1)
        owner.hangup()
        processed=until((tmp_path/'hangup-processed').exists,2)
        queued=array.array('i',[0])
        fcntl.ioctl(owner.master,termios.FIONREAD,queued,True)
        witness={'proof_layer':'host-local-FD-only','real_SIP_peer_started':False,
                 'child_pid':owner.proc.pid,'child_alive':owner.proc.poll() is None,
                 'PTY_master_queued_bytes':queued[0],'output_finished':(tmp_path/'output-finished').exists(),
                 'reader_alive':getattr(owner,'_console_reader',None).is_alive() if hasattr(owner,'_console_reader') else None,
                 'collected_characters':len(owner.output),
                 'hangup_processed_without_application_read':processed}
        (tmp_path/'witness.json').write_text(json.dumps(witness,indent=2)+'\n')
        assert processed,'Owned PTY console stalled before hangup: '+json.dumps(witness)
        assert (tmp_path/'output-finished').exists()
    finally:
        # RED cleanup drains explicitly after the assertion was observed.
        deadline=time.monotonic()+3
        while not (tmp_path/'output-finished').exists() and time.monotonic()<deadline:
            owner.read()
            time.sleep(.005)
        master=owner.master
        owner.close()
        assert owner.proc.poll() is not None
        reader=getattr(owner,'_console_reader',None)
        if reader is not None:
            assert not reader.is_alive()
        with pytest.raises(OSError):
            os.fstat(master)
        if reader is not None:
            owner.close()
