"""Execute composed display/content lambdas with native media-state transitions.

Only LVGL images, text publication and unrelated lighting/pagination actions are
stubbed. The phase projection, TTS content, image callbacks and artwork writer
come from each shipped profile through ESPHome's real package composition.
"""

import json
from pathlib import Path
import subprocess

import pytest

from tests.native_profile import action_id, profile


PROFILES = (
    "full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml",
    "full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml",
    "full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml",
)


def _actions(actions):
    """Keep executable content decisions; omit unrelated UI work explicitly."""
    result = []
    for action in actions:
        if "lambda" in action:
            result.append(action["lambda"])
        elif "if" in action:
            branch = action["if"]
            condition = branch["condition"]["lambda"]
            result.append("if ([&]() {" + condition + "}()) {" + _actions(branch["then"]) + "}")
        elif "script.execute" in action:
            name = action_id(action, "script.execute")
            if name == "apply_media_artwork":
                result.append("apply_media_artwork.execute();")
            elif name == "set_idle_or_mute_phase":
                result.append("project();")
        elif "globals.set" in action:
            setting = action["globals.set"]
            if setting["id"] == "runtime_response_text_ready":
                result.append("runtime_response_text_ready = " + setting["value"] + ";")
        elif "text_sensor.template.publish" in action:
            setting = action["text_sensor.template.publish"]
            result.append(setting["id"] + ".publish_state(" + json.dumps(setting["state"]) + ");")
    return "\n".join(result)


@pytest.fixture(scope="module", params=PROFILES, ids=["s3-round", "p4-portrait", "p4-landscape"])
def artwork_binary(request, tmp_path_factory):
    config = profile(request.param)
    scripts = {item["id"]: item for item in config["script"]}
    artwork = next(item for item in config["image"] if item.get("id") == "sendspin_artwork_slot")
    player = next(item for item in config["media_player"] if item["id"] == "speaker_media_player")
    constants = "\n".join(
        f"constexpr int {key} = {value};"
        for key, value in config["substitutions"].items()
        if key.startswith("ui_state_") or key.startswith("voice_assist_")
    )
    source = r"""
#include <cassert>
#include <functional>
#include <string>
#define id(name) name
namespace esphome::image { struct Image { int source; }; }
namespace media_player {
enum MediaPlayerState { MEDIA_PLAYER_STATE_IDLE, MEDIA_PLAYER_STATE_PLAYING,
                       MEDIA_PLAYER_STATE_PAUSED, MEDIA_PLAYER_STATE_ANNOUNCING };
}
using esphome::image::Image;
Image neutral{1}, happy{2}, angry{3}, cover{4};
Image *mood_neutral=&neutral, *mood_happy=&happy, *mood_angry=&angry, *sendspin_cover_art=&cover;
struct Widget { Image *source=&neutral; int invalidations=0; } background;
Widget *mood_bg=&background;
void lv_img_set_src(Widget *target, Image *source) { target->source=source; }
Image *lv_img_get_src(Widget *target) { return target->source; }
void lv_obj_invalidate(Widget *target) { ++target->invalidations; }
struct Sensor { std::string state; void publish_state(const std::string &text) { state=text; } } text_response, text_request;
struct Switch { bool state=false; } mute;
struct Player { media_player::MediaPlayerState state=media_player::MEDIA_PLAYER_STATE_IDLE; } speaker_media_player;
struct Runtime { bool announcement=false; bool is_activity_active(const char *) const { return announcement; } } runtime;
int ui_state=0, voice_assistant_phase=1, current_mode=0, runtime_visual_source=1;
bool runtime_response_text_ready=false, sendspin_artwork_available=false;
void apply_artwork_impl(); void project();
struct Script { void execute() { apply_artwork_impl(); } } apply_media_artwork;
"""
    source += constants
    source += "\nvoid apply_artwork_impl() {\n" + _actions(scripts["apply_media_artwork"]["then"]) + "\n}\n"
    source += "void project() {\n" + scripts["runtime_project_ui_phase"]["then"][0]["lambda"] + "\n}\n"
    # The first action is the production text/avatar selection on all renderers.
    source += "void tts_start(std::string text) {\n" + scripts["ui_tts_started"]["then"][0]["lambda"] + "\n}\n"
    source += "void content_end() {\n" + _actions(scripts["ui_va_end"]["then"]) + "\n}\n"
    source += "void player_callback() {\n" + _actions(player.get("on_state", [])) + "\n}\n"
    for callback in ("display", "clear", "error"):
        source += f"void image_{callback}() {{\n" + _actions(artwork["on_image_" + callback]) + "\n}\n"
    source += r"""
void player_state(media_player::MediaPlayerState state) {
  speaker_media_player.state=state;
  runtime.announcement=(state==media_player::MEDIA_PLAYER_STATE_ANNOUNCING);
  player_callback();
}
void music() {
  image_display();
  player_state(media_player::MEDIA_PLAYER_STATE_PLAYING);
  ui_state=ui_state_media; project();
  assert(background.source==&cover && runtime_visual_source==4);
}
void response() {
  ui_state=ui_state_va_listening; project();
  ui_state=ui_state_va_thinking; project();
  ui_state=ui_state_va_replying; project();
  // Intent progress can select responding before TTS_START supplies any text.
  assert(background.source==&neutral && runtime_visual_source==1);
  tts_start(":-) Response text");
  assert(background.source==&happy && runtime_visual_source==2);
  assert(text_response.state=="Response text" && runtime_response_text_ready);
}
void assert_response() {
  assert(background.source==&happy && runtime_visual_source==2);
  assert(text_response.state=="Response text" && runtime_response_text_ready);
}
int main(int argc, char **argv) {
  assert(argc==2);
  const std::string scenario=argv[1];
  if (scenario=="direct_tts") {
    music();
    // The semantic display policy stays media throughout this interruption.
    player_state(media_player::MEDIA_PLAYER_STATE_ANNOUNCING);
    assert(ui_state==ui_state_media && voice_assistant_phase==voice_assist_replying_phase_id);
    assert(background.source==&neutral && runtime_visual_source==1);
    image_display(); image_clear(); image_error(); image_display();
    assert(background.source==&neutral && runtime_visual_source==1);
    // No policy change or new image is required to restore the same track.
    player_state(media_player::MEDIA_PLAYER_STATE_PLAYING);
    assert(background.source==&cover && runtime_visual_source==4);
  } else if (scenario=="assist_pipeline_first" || scenario=="assist_audio_first") {
    music(); response();
    player_state(media_player::MEDIA_PLAYER_STATE_ANNOUNCING);
    image_display(); assert_response();
    image_clear(); assert_response();
    image_error(); assert_response();
    image_display(); assert_response();
    // A pipeline end before playback drain retains the committed responding
    // policy. The reducer's two completion orders have their own runtime tests.
    if (scenario=="assist_pipeline_first") { project(); assert_response(); }
    player_state(media_player::MEDIA_PLAYER_STATE_PLAYING);
    assert_response();
    if (scenario=="assist_audio_first") { project(); assert_response(); }
    // Exercise both possible ordering effects: display projection before the
    // response-inactive hook must still restore music after content cleanup.
    ui_state=ui_state_media; project(); content_end();
    assert(!runtime_response_text_ready && text_response.state.empty());
    assert(background.source==&cover && runtime_visual_source==4);
  } else if (scenario=="pending_tts_text") {
    music();
    // Content runs synchronously before its deferred runtime observation.
    tts_start(":-) Response text");
    image_display(); player_callback(); assert_response();
    ui_state=ui_state_va_replying; project(); assert_response();
  } else if (scenario=="cover_cleared_during_assist") {
    music(); response();
    player_state(media_player::MEDIA_PLAYER_STATE_ANNOUNCING);
    image_clear(); assert_response();
    player_state(media_player::MEDIA_PLAYER_STATE_PLAYING);
    ui_state=ui_state_media; project(); content_end();
    assert(background.source==&neutral && runtime_visual_source==1);
    image_display();
    assert(background.source==&cover && runtime_visual_source==4);
  } else if (scenario=="paused_and_idle") {
    music();
    player_state(media_player::MEDIA_PLAYER_STATE_PAUSED);
    image_display();
    assert(background.source==&neutral && runtime_visual_source==1);
    ui_state=ui_state_idle; project();
    image_display();
    assert(background.source==&neutral && runtime_visual_source==1);
    player_state(media_player::MEDIA_PLAYER_STATE_PLAYING);
    ui_state=ui_state_media; project();
    assert(background.source==&cover && runtime_visual_source==4);
  } else if (scenario=="replacement_cover") {
    music();
    // Sendspin replaces decoded pixels behind the same current_image object.
    const int before=background.invalidations;
    image_display();
    assert(background.source==&cover && background.invalidations>before);
    image_clear();
    assert(background.source==&neutral && runtime_visual_source==1);
    image_display();
    assert(background.source==&cover && runtime_visual_source==4);
    image_error();
    assert(background.source==&neutral && runtime_visual_source==1);
  } else if (scenario=="stable_idle_projection") {
    ui_state=ui_state_idle; project();
    const int before=background.invalidations;
    for (unsigned n=0; n<100; ++n) project();
    assert(background.source==&neutral && runtime_visual_source==1);
    assert(background.invalidations==before);
  } else if (scenario=="other_owner_callbacks") {
    tts_start(":-) Response text");
    for (int owner : {ui_state_va_listening, ui_state_va_thinking, ui_state_va_replying,
                      ui_state_call_ringing, ui_state_call_in_call, ui_state_mic_muted,
                      ui_state_no_ha, ui_state_no_va}) {
      ui_state=owner;
      player_state(media_player::MEDIA_PLAYER_STATE_PLAYING);
      image_display(); assert_response();
      image_clear(); assert_response();
      image_error(); assert_response();
    }
  } else { return 2; }
}
"""
    work = tmp_path_factory.mktemp("artwork-presentation")
    cpp = work / "artwork.cpp"
    cpp.write_text(source)
    binary = work / "artwork"
    subprocess.run(
        ["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", str(cpp), "-o", str(binary)],
        check=True, capture_output=True, text=True,
    )
    return binary


@pytest.mark.parametrize("scenario", [
    "direct_tts", "assist_pipeline_first", "assist_audio_first", "pending_tts_text",
    "cover_cleared_during_assist", "paused_and_idle", "other_owner_callbacks",
    "replacement_cover", "stable_idle_projection",
])
def test_artwork_respects_committed_content_owner(artwork_binary: Path, scenario):
    subprocess.run([str(artwork_binary), scenario], check=True, capture_output=True, text=True)
