"""Execute the actual ringtone policy without stopping an unrelated source."""
from dataclasses import dataclass
from pathlib import Path

from esphome import yaml_util
import pytest

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Phone:
    ringing: bool = False
    loop_running: bool = False
    file_state: str = "IDLE"
    announcement: str | None = "imap"
    stops: int = 0


def condition(value, phone):
    if "voip_stack.is_ringing" in value:
        return phone.ringing
    if "script.is_running" in value:
        assert value["script.is_running"] == "voip_ringing_loop"
        return phone.loop_running
    if "or" in value:
        return any(condition(item, phone) for item in value["or"])
    if "lambda" in value:
        assert value["lambda"].strip() == "return id(ringtone_file_source).get_state() != esphome::media_source::MediaSourceState::IDLE;"
        return phone.file_state != "IDLE"
    raise AssertionError(value)


def execute(actions, phone):
    for action in actions:
        if "if" in action:
            branch = action["if"]
            execute(branch.get("then" if condition(branch["condition"], phone) else "else", []), phone)
        elif "script.execute" in action:
            assert action["script.execute"] == "voip_ringing_loop"
            phone.loop_running = True
        elif "script.stop" in action:
            assert action["script.stop"] == "voip_ringing_loop"
            phone.loop_running = False
        elif "media_player.stop" in action:
            assert action["media_player.stop"]["announcement"] is True
            phone.stops += 1
            phone.announcement = None
        else:
            raise AssertionError(action)


@pytest.mark.parametrize("board", ["waveshare-s3-phone", "spotpear-ball-v2-phone"])
@pytest.mark.parametrize("running,state,source,stops", [
    (False, "IDLE", "imap", 0),
    (False, "IDLE", "other-announcement", 0),
    (True, "IDLE", "ringtone-pending", 1),
    (False, "PLAYING", "ringtone", 1),
    (False, "PAUSED", "ringtone", 1),
    (True, "PLAYING", "ringtone", 1),
])
def test_idle_refresh_stops_only_the_ringtone_owner(board, running, state, source, stops):
    document = yaml_util.load_yaml(ROOT / "packages/features/voip/devices" / f"{board}.yaml")
    status = next(script for script in document["script"]
                  if getattr(script["id"], "value", str(script["id"])) == "update_status")
    phone = Phone(loop_running=running, file_state=state, announcement=source)
    execute(status["then"], phone)
    assert phone.stops == stops
    assert phone.announcement == (None if stops else source)
    assert phone.loop_running is False
