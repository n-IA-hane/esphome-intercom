"""Video suspension preserves the public Wake Word intent in shared YAML."""

from pathlib import Path

import yaml

INTERCOM = Path(__file__).resolve().parents[1]


def test_video_changes_suspension_instead_of_the_user_wake_switch():
    policy = yaml.safe_load((INTERCOM / "packages/voip/video_mww_policy.yaml").read_text())
    scripts = {entry["id"]: entry["then"] for entry in policy["script"]}
    assert scripts["suspend_mww_for_video"] == [
        {"globals.set": {"id": "g_mww_video_suspended", "value": "true"}},
        {"script.stop": "start_mww_when_ready"},
        {"micro_wake_word.stop": None},
    ]
    assert scripts["resume_mww_after_video"] == [
        {"globals.set": {"id": "g_mww_video_suspended", "value": "false"}},
        {"script.execute": "start_mww_when_ready"},
    ]
    text = (INTERCOM / "packages/runtime/mww_safe_start.yaml").read_text()
    for guard in ("!id(g_mww_video_suspended)", "id(g_wake_word_enabled)",
                  "!id(init_in_progress)", "!id(g_ota_in_progress)"):
        assert guard in text
    assert "switch.is_off: mute" in text
    assert "wake_word_switch" not in yaml.safe_dump(policy)
    p4 = (INTERCOM / "packages/voip/p4_full_video_jpeg.yaml").read_text()
    assert "!include video_mww_policy.yaml" in p4
    assert "restore_mww_after_video_call" not in p4
