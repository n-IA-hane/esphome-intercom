"""Validate the existing JPEG runtime setter and its optional settings widget."""
from tests.native_profile import text
from tests.test_lvgl_settings_composition import expanded_scene, page_and_controls


def test_p4_jpeg_quality_uses_existing_camera_owner():
    config = expanded_scene("p4-landscape", "full")
    control = next(n for n in config["number"] if str(n.get("id")) == "p4_camera_jpeg_quality")
    assert (control["min_value"], control["max_value"], control["step"]) == (1, 63, 1)
    assert control["initial_value"] == 10
    assert control["restore_value"] and control["optimistic"]
    assert "set_runtime_jpeg_quality" in text(control["on_value"])
    assert "set_runtime_max_fps" not in text(control)
    camera = config["esp_video_camera"]
    assert camera["device"] == "jpeg" and camera["resolution"] == "800x800"
    assert camera["jpeg_quality"] == 10 and camera["max_framerate"] == 10
    _, widgets = page_and_controls(config)
    slider = widgets["ui_camera_jpeg_quality_slider"]
    assert (slider["min_value"], slider["max_value"]) == (1, 63)
    assert "on_release" in slider and "on_change" not in slider
    assert "p4_camera_jpeg_quality" in text(slider["on_release"])


def test_jpeg_control_is_absent_without_the_p4_jpeg_camera():
    for scene, variant in [("p4-landscape", "no-voip"), ("box360-afe-independent", "full")]:
        config = expanded_scene(scene, variant)
        assert all(str(n.get("id")) != "p4_camera_jpeg_quality" for n in config.get("number", []))
        _, widgets = page_and_controls(config)
        assert "ui_camera_jpeg_quality_slider" not in widgets
        assert "settings_sync_camera_quality" not in text(config["script"])
