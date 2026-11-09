"""Late camera enable preserves an initial bidirectional audio with local camera TX disabled."""
import inspect
from types import SimpleNamespace
import pytest
from tools import sip_video_browser_probe as probe


def test_main_places_late_camera_after_connection():
    source=inspect.getsource(probe.main)
    assert source.index('connected = sample("in_call")')<source.index('_enable_camera_after_audio(')<source.index('flowing = sample("video_flowing")')


def test_phase_plan_preserves_default_and_fixture_behavior():
    assert probe._camera_phase_plan(False,None,False)=={'late':False,'configure_initial':False,'initial':True}
    assert probe._camera_phase_plan(False,'off',False)['initial'] is False
    assert probe._camera_phase_plan(False,'on',True)['initial'] is True
    assert probe._camera_phase_plan(False,None,True)['initial'] is True
    assert probe._camera_phase_plan(True,None,True)=={'late':True,'configure_initial':True,'initial':False}
    assert probe._camera_phase_plan(True,'off',True)['initial'] is False


@pytest.mark.parametrize('stalled',[False,True])
def test_three_seconds_audio_progress_precedes_camera_toggle(stalled):
    events=[]
    class Page:
        def wait_for_function(self,expression,**kwargs):events.append('wait')
        def wait_for_timeout(self,ms):assert ms==3000;events.append('audio3s')
        def evaluate(self,expression,enabled):
            assert expression==probe.CLICK_CAMERA_SEND and enabled is True
            events.append('camera_on');return True
    def sample(label):
        events.append(label)
        progress=20 if label=='audio_before_camera_toggle' or stalled else 50
        return {'card_state':'in_call','video_active':True,'engine_video_active':True,'backend_state':{'send_video':False},
                'engine_stats':{'sent':progress,'received':progress}}
    if stalled:
        with pytest.raises(RuntimeError,match='Audio/TX-off state'):probe._enable_camera_after_audio(Page(),sample,25)
        assert 'camera_on' not in events
    else:
        probe._enable_camera_after_audio(Page(),sample,25)
        assert events==['wait','audio_before_camera_toggle','audio3s','audio_before_camera_toggle_after_3s','camera_on','wait','camera_enabled_after_audio']


def test_late_camera_rejects_ambiguous_initial_on_before_auth(monkeypatch):
    import sys
    monkeypatch.setattr(sys,'argv',['probe','--send-camera','--camera-send','on'])
    with pytest.raises(SystemExit) as error:probe.main()
    assert error.value.code==2


def test_already_enabled_camera_tx_is_not_a_valid_late_start():
    class Page:
        def wait_for_function(self,*args,**kwargs):pass
        def wait_for_timeout(self,ms):pass
        def evaluate(self,*args):raise AssertionError("must not toggle an already enabled TX")
    def sample(label):
        n=10 if label=="audio_before_camera_toggle" else 30
        return {"card_state":"in_call","backend_state":{"send_video":True},"engine_stats":{"sent":n,"received":n,"video":{"sent":3}}}
    with pytest.raises(RuntimeError,match="Audio/TX-off state"):
        probe._enable_camera_after_audio(Page(),sample,25)
