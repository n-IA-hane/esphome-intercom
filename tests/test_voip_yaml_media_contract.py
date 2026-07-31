"""Contracts shared by maintained ESPHome VoIP media profiles."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
YAMLS = ROOT / "yamls"

PHYSICAL_AUDIO_STACK_PROFILES = (
    "experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml",
    "full-experience/dual-bus/generic-s3-full-aec.yaml",
    "full-experience/single-bus/generic-s3-full-aec.yaml",
    "full-experience/single-bus/spotpear-ball-v2-full-afe.yaml",
    "full-experience/single-bus/waveshare-p4-touch-full-afe-landscape.yaml",
    "full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml",
    "full-experience/single-bus/waveshare-s3-full-afe.yaml",
    "untested/generic-s3-full-afe.yaml",
    "voip-only/dual-bus/generic-s3-voip.yaml",
    "voip-only/single-bus/generic-s3-voip.yaml",
    "voip-only/single-bus/spotpear-ball-v2-voip.yaml",
)

SPOTPEAR_FULL_AFE = (
    YAMLS / "full-experience" / "single-bus" / "spotpear-ball-v2-full-afe.yaml"
)
P4_LANDSCAPE_FULL_AFE = (
    YAMLS
    / "full-experience"
    / "single-bus"
    / "waveshare-p4-touch-full-afe-landscape.yaml"
)


def _voip_stack_block(text: str) -> str:
    match = re.search(r"(?m)^voip_stack:\n", text)
    if match is None:
        return ""
    tail = text[match.end() :]
    next_top_level = re.search(r"(?m)^\S", tail)
    return tail if next_top_level is None else tail[: next_top_level.start()]


def _format_section(block: str, name: str) -> str:
    match = re.search(rf"(?m)^    {re.escape(name)}:\n", block)
    if match is None:
        return ""
    tail = block[match.end() :]
    next_peer = re.search(r"(?m)^    \S", tail)
    return tail if next_peer is None else tail[: next_peer.start()]


def _has_s16le_mono_format(section: str, sample_rate: int, frame_ms: int) -> bool:
    entries = re.split(r"(?m)^      - ", section)
    return any(
        re.search(rf"(?m)^sample_rate:\s*{sample_rate}\s*$", entry)
        and re.search(r"(?m)^        pcm_format:\s*s16le\s*$", entry)
        and re.search(r"(?m)^        channels:\s*1\s*$", entry)
        and re.search(rf"(?m)^        frame_ms:\s*{frame_ms}\s*$", entry)
        for entry in entries[1:]
    )


def test_resampling_profiles_accept_direct_esp_16khz_10ms() -> None:
    """Every explicit multi-format RX profile accepts the ESP direct-call floor."""
    missing: list[str] = []
    for path in sorted(YAMLS.rglob("*.yaml")):
        if ".esphome" in path.parts:
            continue
        block = _voip_stack_block(path.read_text())
        rx_formats = _format_section(block, "rx_formats")
        if not rx_formats:
            continue
        if not _has_s16le_mono_format(rx_formats, 16000, 10):
            missing.append(str(path.relative_to(ROOT)))

    assert not missing, (
        "VoIP profiles with explicit rx_formats must retain the direct ESP-to-ESP "
        "16000/s16le/mono/10ms compatibility floor:\n" + "\n".join(missing)
    )


def test_physical_audio_stack_speakers_bound_silent_tx_lifecycle() -> None:
    """Mixer drain must eventually release the physical I2S output."""
    missing: list[str] = []
    for relative in PHYSICAL_AUDIO_STACK_PROFILES:
        text = (YAMLS / relative).read_text()
        if not re.search(
            r"(?ms)^  - platform: esp_audio_stack\n"
            r"(?:(?!^  - platform:).)*?^    timeout:\s*1s\s*$",
            text,
        ):
            missing.append(relative)

    assert not missing, (
        "Physical esp_audio_stack speakers behind mixer/resampler sources need a "
        "bounded lifecycle timeout:\n" + "\n".join(missing)
    )


def test_ws3_uses_native_esp32_partition_configuration() -> None:
    """ESPHome 2026.7 native IDF builds ignore the legacy PlatformIO key."""
    relative = (
        "experimental/waveshare-s3-touch-lcd-1.85c/"
        "waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml"
    )
    text = (YAMLS / relative).read_text()

    assert "partitions: partitions_16mb_huge_factory.csv" in text
    assert not re.search(r"(?m)^\s*board_build\.partitions:\s*", text)


def test_spotpear_round_dialer_preserves_memory_and_routing_contracts() -> None:
    text = SPOTPEAR_FULL_AFE.read_text()
    dialer_match = re.search(
        r"(?ms)^    - id: call_dialer_page\n"
        r"(?P<body>.*?)(?=^    - id: settings_continuous_page\n)",
        text,
    )

    assert re.search(r"(?m)^  buffer_size: 50%$", text)
    assert re.search(
        r"(?ms)^runtime_controller:\n"
        r"  id: runtime\n"
        r"  storage_in_psram: true\n",
        text,
    )
    assert "circle_half_chord" not in text
    assert re.search(
        r"(?ms)^  - id: voip_dial_buffer\n"
        r'    type: "std::array<char, 33>"\n'
        r".*?^  - id: voip_dial_length\n"
        r"    type: uint8_t\n",
        text,
    )

    assert dialer_match is not None
    dialer = dialer_match.group("body")
    assert dialer.count("- buttonmatrix:") == 4
    for widget_id in (
        "call_dial_row_123",
        "call_dial_row_456",
        "call_dial_row_789",
        "call_dial_row_del0hash",
        "call_dial_call_btn",
    ):
        assert f"id: {widget_id}" in dialer

    assert '- text: "\\U000F006E"' in dialer
    assert "script.execute: voip_dial_backspace" in dialer
    assert 'if (x == 2) return \'#\';' in dialer
    assert '- text: "*"' not in dialer
    assert '"\\U000F006E" # backspace' in text

    assert "id(phone).get_contact_count()" in text
    assert "get_contacts_csv()" not in text


def test_spotpear_layout_and_aec_sync_use_runtime_contracts() -> None:
    text = SPOTPEAR_FULL_AFE.read_text()
    esphome_block = text.split("\nesp32:", 1)[0]
    lvgl_block = text.split("\nlvgl:", 1)[1].split("\nglobals:", 1)[0]

    assert "script.execute: compute_layout_metrics" not in esphome_block
    assert "script.execute: reposition_widgets" not in esphome_block
    assert re.search(
        r"(?ms)^  on_boot:\n"
        r"    - script\.execute: compute_layout_metrics\n"
        r"    - script\.execute: reposition_widgets\n",
        lvgl_block,
    )
    assert re.search(
        r"(?ms)^            options:\n"
        r"              - sr_low_cost\n"
        r"              - sr_high_perf\n"
        r"              - fd_low_cost\n"
        r"              - fd_high_perf\n",
        text,
    )


def test_p4_full_profile_is_audio_only_with_native_ha_camera() -> None:
    text = P4_LANDSCAPE_FULL_AFE.read_text()
    block = _voip_stack_block(text)

    assert "components: [speaker, voice_assistant, mipi_dsi, esp_video_camera]" in text
    assert "p4_sip_video" not in text
    assert "esp_h264_video_source" not in text
    assert "esp_jpeg_video_source" not in text
    assert "p4_video_renderer" not in text
    assert not re.search(r"(?m)^  video:", block)
    assert not re.search(r"(?m)^  video_debug:", block)
    assert "call_video_page" not in text
    assert "call_video_surface" not in text
    assert "id(p4_video)" not in text
    assert "video_send_switch" not in text
    assert "ui_video_send_sw" not in text
    assert re.search(
        r"(?ms)^esp_video_camera:\n"
        r".*?^  id: p4_camera\n"
        r'.*?^  name: "P4 Camera"\n'
        r".*?^  i2c_id: internal_i2c\n"
        r".*?^  device: jpeg\n"
        r".*?^  resolution: 800x800\n"
        r".*?^  jpeg_quality: 10\n"
        r".*?^  max_framerate: 5\n"
        r".*?^  rotation: 270\n",
        text,
    )
    assert re.search(
        r"(?m)^  audio_task_stacks_in_psram: true$", block
    )
    assert not re.search(r"(?m)^safe_mode:", text)
    assert "id(phone).get_contact_count()" in text
    assert "get_contacts_csv()" not in text
    assert re.search(
        r"(?ms)^        - number\.set:\n"
        r"            id: master_volume\n"
        r"            value: 1\n",
        text,
    )

    hosted = (
        ROOT / "packages" / "board" / "esp32p4_c6_sdio.yaml"
    ).read_text()
    assert 'CONFIG_ESP_HOSTED_DFLT_TASK_FROM_SPIRAM: "y"' in hosted
    assert 'CONFIG_SPIRAM_ALLOW_STACK_EXTERNAL_MEMORY: "y"' in hosted
    assert 'CONFIG_ESP_HOSTED_SDIO_TX_Q_SIZE: "20"' in hosted
    assert 'CONFIG_ESP_HOSTED_SDIO_RX_Q_SIZE: "20"' in hosted


def test_p4_sip_only_uses_espressif_hosted_video_receive_depths() -> None:
    board = (
        ROOT
        / "packages"
        / "board"
        / "waveshare_p4_touch_10_1_videophone.yaml"
    ).read_text()

    assert 'CONFIG_WIFI_RMT_STATIC_RX_BUFFER_NUM: "16"' in board
    assert 'CONFIG_WIFI_RMT_DYNAMIC_RX_BUFFER_NUM: "64"' in board
    assert 'CONFIG_WIFI_RMT_DYNAMIC_TX_BUFFER_NUM: "64"' in board
    assert 'CONFIG_WIFI_RMT_TX_BA_WIN: "32"' in board
    assert 'CONFIG_WIFI_RMT_RX_BA_WIN: "32"' in board
    assert 'CONFIG_LWIP_TCPIP_RECVMBOX_SIZE: "64"' in board
    base = (
        YAMLS
        / "voip-only"
        / "single-bus"
        / "waveshare-p4-touch-videophone-base.yaml"
    ).read_text()
    assert re.search(
        r"(?ms)^        - number\.set:\n"
        r"            id: master_volume\n"
        r"            value: 1\n",
        base,
    )
    assert 'p4_afe_task_priority: "18"' in base
    assert 'p4_afe_feed_task_priority: "18"' in base
    assert 'p4_afe_fetch_task_priority: "18"' in base
    assert (
        "  task_priority: ${p4_afe_task_priority}\n"
        "  feed_task_priority: ${p4_afe_feed_task_priority}\n"
        "  fetch_task_priority: ${p4_afe_fetch_task_priority}\n"
    ) in board


def test_p4_h264_prioritizes_afe_without_changing_jpeg_scheduler() -> None:
    profile_dir = YAMLS / "voip-only" / "single-bus"
    h264 = (
        profile_dir / "waveshare-p4-touch-videophone-h264.yaml"
    ).read_text()
    jpeg = (
        profile_dir / "waveshare-p4-touch-videophone-jpeg.yaml"
    ).read_text()

    assert 'p4_afe_task_priority: "20"' in h264
    assert 'p4_afe_feed_task_priority: "19"' in h264
    assert 'p4_afe_fetch_task_priority: "20"' in h264
    assert "p4_afe_task_priority" not in jpeg
    assert "p4_afe_feed_task_priority" not in jpeg
    assert "p4_afe_fetch_task_priority" not in jpeg


def test_p4_codec_profiles_have_isolated_generated_builds() -> None:
    profile_dir = YAMLS / "voip-only" / "single-bus"
    jpeg = (
        profile_dir / "waveshare-p4-touch-videophone-jpeg.yaml"
    ).read_text()
    h264 = (
        profile_dir / "waveshare-p4-touch-videophone-h264.yaml"
    ).read_text()
    full = P4_LANDSCAPE_FULL_AFE.read_text()

    assert (
        "build_path: .esphome/build/waveshare-p4-touch-videophone-jpeg"
        in jpeg
    )
    assert (
        "build_path: .esphome/build/waveshare-p4-touch-videophone-h264"
        in h264
    )
    assert (
        "build_path: .esphome/build/waveshare-p4-touch-full-afe-landscape"
        in full
    )


def test_p4_production_profiles_keep_video_debug_off_and_safe_mode_on() -> None:
    profile_dir = YAMLS / "voip-only" / "single-bus"
    dedicated = (
        profile_dir / "waveshare-p4-touch-videophone-base.yaml"
    ).read_text()
    full = P4_LANDSCAPE_FULL_AFE.read_text()
    full_jpeg = (
        YAMLS
        / "full-experience"
        / "single-bus"
        / "waveshare-p4-touch-full-afe-landscape-sip-jpeg.yaml"
    ).read_text()

    assert "video_debug:" not in dedicated
    assert "video_debug:" not in full_jpeg
    assert not re.search(r"(?m)^safe_mode:", dedicated)
    assert not re.search(r"(?m)^safe_mode:", full)
    assert not re.search(r"(?m)^ota:", full)
    assert "ota_maintenance:" in full


def test_p4_idle_animation_uses_rendered_page_state() -> None:
    text = P4_LANDSCAPE_FULL_AFE.read_text()
    lifecycle = text[
        text.index("      # Animation lifecycle") :
        text.index("      # Self-heal: ensure MWW is active when idle")
    ]

    assert 'id(runtime_rendered_page).rfind("main:idle:", 0) == 0' in lifecycle
    assert "lv_disp_get_scr_act" not in lifecycle
    for rendered_page in ("settings", "no_wifi", "no_ha", "timer_finished"):
        assert f'id(runtime_rendered_page) = "{rendered_page}";' in text


def test_p4_idle_animation_has_generic_call_and_page_gates() -> None:
    text = P4_LANDSCAPE_FULL_AFE.read_text()
    main_page_start = text.index("    - id: main_page")
    main_page = text[main_page_start : text.index("      widgets:", main_page_start)]
    call_started = text[
        text.index("  - id: !extend ui_call_started") :
        text.index("  - id: !extend ui_call_dest_ringing")
    ]
    call_ended = text[
        text.index("  - id: !extend ui_call_ended") :
        text.index("  # Main display rendering")
    ]

    assert "on_unload:\n        - script.stop: ai_animation_loop" in main_page
    assert "- script.stop: ai_animation_loop" in call_started
    assert 'id: current_mode\n          value: "0"' in call_ended
    assert call_ended.rindex("- script.execute: draw_display") > call_ended.index("delay: 4s")


def test_p4_idle_assistant_shows_selected_wake_word() -> None:
    text = P4_LANDSCAPE_FULL_AFE.read_text()

    assert 'text: "Push to talk or say:"' in text
    assert "id: va_wake_word_label" in text
    assert "id(mww).get_wake_words()" in text
    assert "model->is_enabled()" in text
    assert "model->get_wake_word()" in text


def test_p4_full_jpeg_video_page_has_dedicated_lifecycle() -> None:
    full_jpeg = (
        YAMLS
        / "full-experience"
        / "single-bus"
        / "waveshare-p4-touch-full-afe-landscape-sip-jpeg.yaml"
    ).read_text()
    full = P4_LANDSCAPE_FULL_AFE.read_text()

    assert "on_first_frame:\n    - script.execute: show_call_video_page" in full_jpeg
    assert "on_video_ended:\n    - script.execute: hide_call_video_page" in full_jpeg
    assert "- script.stop: draw_display" in full_jpeg
    assert "- script.stop: ai_animation_loop" in full_jpeg
    assert 'id(runtime_rendered_page) = "call_video";' in full_jpeg
    assert 'id(runtime_rendered_page) == "call_video"' in full
    assert "id(phone).is_in_call()" in full
    assert 'id: current_mode\n          value: "0"' in full


def test_p4_videophone_contact_navigation_waits_for_owner_callback() -> None:
    """The SIP owner publishes the new contact; UI actions must not read stale state."""
    ui = (ROOT / "packages" / "lvgl" / "p4_videophone_ui.yaml").read_text()
    base = (
        YAMLS
        / "voip-only"
        / "single-bus"
        / "waveshare-p4-touch-videophone-base.yaml"
    ).read_text()

    navigation = ui[ui.index("on_swipe_left:") : ui.index("id: videophone_call_button")]
    assert navigation.count("voip_stack.next_contact:") == 1
    assert navigation.count("voip_stack.prev_contact:") == 2
    assert "get_current_destination()" not in navigation
    assert "script.execute: update_status" not in navigation
    assert re.search(
        r"(?ms)^  on_destination_changed:\n"
        r"    - lambda: id\(videophone_peer\) = destination;\n"
        r"    - script.execute: update_status\n",
        base,
    )


def test_p4_camera_applies_initial_jpeg_quality_with_extended_controls() -> None:
    camera_cpp = (
        ROOT / "esphome" / "components" / "esp_video_camera" / "esp_video_camera.cpp"
    ).read_text()
    start = camera_cpp.index("bool ESPVideoCamera::start_jpeg_pipeline_()")
    end = camera_cpp.index("bool ESPVideoCamera::", start + 1)
    jpeg_pipeline = camera_cpp[start:end]

    assert 'this->jpeg_quality_, "jpeg_quality"' in jpeg_pipeline
    assert "gate_set_ext_ctrl(" in jpeg_pipeline
    assert "ioctl(this->jpeg_fd_, VIDIOC_S_CTRL" not in jpeg_pipeline


def test_p4_video_workers_are_event_driven_and_use_bounded_direct_display() -> None:
    camera_cpp = (
        ROOT / "esphome" / "components" / "esp_video_camera" / "esp_video_camera.cpp"
    ).read_text()
    camera_header = (
        ROOT / "esphome" / "components" / "esp_video_camera" / "esp_video_camera.h"
    ).read_text()
    renderer_cpp = (
        ROOT
        / "esphome"
        / "components"
        / "p4_video_renderer"
        / "p4_video_renderer.cpp"
    ).read_text()
    renderer_header = (
        ROOT
        / "esphome"
        / "components"
        / "p4_video_renderer"
        / "p4_video_renderer.h"
    ).read_text()
    source_cpp = (
        ROOT
        / "esphome"
        / "components"
        / "esp_h264_video_source"
        / "esp_h264_video_source.cpp"
    ).read_text()
    source_header = (
        ROOT
        / "esphome"
        / "components"
        / "esp_h264_video_source"
        / "esp_h264_video_source.h"
    ).read_text()

    assert 'this->set_timeout("capture_linger", LINGER_MS' in camera_cpp
    assert "this->enable_loop_soon_any_context();" in camera_cpp
    assert "this->disable_loop();" in camera_cpp
    capture_start = camera_cpp[camera_cpp.index("bool ESPVideoCamera::start_direct_capture_") :]
    assert "O_RDWR | O_NONBLOCK" not in capture_start
    capture_hot_path = camera_cpp[
        camera_cpp.index("void ESPVideoCamera::capture_task_run_()") :
        camera_cpp.index("void ESPVideoCamera::deliver_raw_frame_(")
    ]
    assert "vTaskDelay" not in capture_hot_path
    assert "ppa_do_scale_rotate_mirror" in camera_cpp
    assert "this->ppa_transform_required_" in camera_cpp
    assert "operation.scale_x = this->ppa_scale_x_;" in camera_cpp
    assert "operation.scale_y = this->ppa_scale_y_;" in camera_cpp
    assert "hardware_framerate_active_" in camera_cpp
    assert "this->configure_capture_framerate_()" in camera_cpp
    assert "fmt.fmt.pix.pixelformat != pixelformat" in camera_cpp
    assert "actual_stride != packed_stride" in camera_cpp
    assert "if (!this->has_consumers_())" in camera_cpp
    assert "MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT" in camera_cpp
    assert "copy = (uint8_t *) heap_caps_malloc(length, MALLOC_CAP_8BIT)" not in camera_cpp
    assert "std::atomic<uint8_t> references{2};" in camera_cpp
    assert "release_video_init_params(p);" in camera_cpp
    assert "params->config.csi = uvc_only ? nullptr : &params->csi_config;" in camera_cpp
    assert "schedule_capture_retry_();" in camera_cpp
    assert 'this->set_timeout("capture_retry", CAPTURE_RETRY_MS' in camera_cpp
    assert "MAX_CAPTURE_RETRIES" in camera_header
    assert "void on_shutdown() override;" in camera_header
    assert "capture_task_running_" in camera_header
    assert "capture_task_done_storage_" in camera_header
    assert "CAPTURE_STOP_TIMEOUT_MS = 3000" in camera_header
    assert "void ESPVideoCamera::on_shutdown()" in camera_cpp
    assert "esp_video_deinit()" in camera_cpp
    assert "Camera pipeline stopped cleanly" in camera_cpp
    assert "rotated_rgb565_capacity_" in camera_header
    assert "capture_prepared_" in camera_header
    assert "bool ESPVideoCamera::resume_capture_()" in camera_cpp
    assert (
        "this->ppa_transform_required_ &&\n"
        "        (this->ppa_srm_ == nullptr || this->rotated_rgb565_ == nullptr)"
        in camera_cpp
    )
    assert "bool ESPVideoCamera::suspend_capture_()" in camera_cpp
    assert "bool ESPVideoCamera::stop_capture_()" in camera_cpp
    assert "capture_streaming_" in camera_header
    assert "static constexpr int kPpaScaleUnits = 16" in renderer_cpp
    assert "container_width * kPpaScaleUnits / content_width" in renderer_cpp
    assert "container_height * kPpaScaleUnits / content_height" in renderer_cpp
    assert "const auto output_fits" in renderer_cpp
    assert "this->surface_capacity_bytes_" in renderer_cpp
    assert "config.scale_x = scale;" in renderer_cpp
    assert "config.scale_y = scale;" in renderer_cpp
    assert "jpeg_output_streaming_" in camera_header
    assert "jpeg_capture_streaming_" in camera_header
    assert (
        "while (this->capture_task_running_.load(std::memory_order_acquire) &&\n"
        "           this->capture_wanted_.load(std::memory_order_acquire) &&\n"
        "           !this->capture_faulted_.load(std::memory_order_acquire))"
        in camera_cpp
    )
    assert "Camera teardown deferred: V4L2 queue still active" in camera_cpp
    assert "REQBUFS(0) failed" in camera_cpp
    assert "native_capture_fps_" in camera_header
    assert "release.count = 0;" in camera_cpp
    assert (
        "ulTaskNotifyTake(pdTRUE, portMAX_DELAY);" in camera_cpp
    )
    assert (
        "this->rotated_rgb565_capacity_ < required_alloc_size"
        in camera_cpp
    )
    release_rotation = camera_cpp[
        camera_cpp.index("bool ESPVideoCamera::release_rotation_()") :
        camera_cpp.index("bool ESPVideoCamera::setup_capture_buffers_()")
    ]
    assert "heap_caps_free(this->rotated_rgb565_)" not in release_rotation
    assert (
        "if (!this->hardware_framerate_active_.load("
        "std::memory_order_acquire))"
    ) in camera_cpp
    assert "RawVideoFrameConsumer" in camera_header
    assert "register_raw_frame_consumer" in camera_cpp
    assert "start_raw_frame_consumer" in camera_cpp
    assert "stop_raw_frame_consumer" in camera_cpp
    assert "this->is_hw_jpeg_ || this->is_raw_csi_" in camera_cpp
    assert "if (this->is_raw_csi_)\n    return true;" in camera_cpp
    assert "if (this->is_raw_csi_ || !jpeg_wanted)" in camera_cpp
    assert camera_cpp.index("this->deliver_raw_frame_(") < camera_cpp.index(
        "if (this->is_raw_csi_ || !jpeg_wanted)"
    )
    assert camera_cpp.index("this->deliver_raw_frame_(") < camera_cpp.index(
        "if (this->ppa_transform_required_)"
    )
    assert "this->rotation_degrees_" in camera_cpp

    assert "ulTaskNotifyTake(pdTRUE, portMAX_DELAY);" in renderer_cpp
    assert "xTaskNotifyGive(this->rx_task_handle_)" in renderer_cpp
    assert "this->pending_surface_.load(std::memory_order_acquire) >= 0" in renderer_cpp
    assert "access_unit.timestamp_90khz -" in renderer_cpp
    assert "static constexpr uint8_t kTaskPriority = 17;" in renderer_header
    assert (
        "this, kTaskPriority, 1, this->task_stacks_in_psram_"
        in renderer_cpp
    )
    assert (
        "static constexpr size_t kMaxAccessUnitBytes = 128 * 1024;"
        in renderer_header
    )
    assert (
        "static constexpr size_t kH264AccessUnitQueueDepth = 8;"
        in renderer_header
    )
    assert "xQueueCreateStatic(" in renderer_cpp
    assert "xQueueSend(this->h264_au_queue_" in renderer_cpp
    assert "xQueueReceive(this->h264_au_queue_" in renderer_cpp
    assert "A full bounded queue means the decoder has fallen behind." in renderer_cpp
    assert "xRingbuffer" not in renderer_cpp
    assert "RingbufHandle_t" not in renderer_header
    renderer_config = (
        ROOT
        / "esphome"
        / "components"
        / "p4_video_renderer"
        / "__init__.py"
    ).read_text()
    source_config = (
        ROOT
        / "esphome"
        / "components"
        / "esp_h264_video_source"
        / "__init__.py"
    ).read_text()
    camera_config = (
        ROOT
        / "esphome"
        / "components"
        / "esp_video_camera"
        / "__init__.py"
    ).read_text()
    assert 'add_idf_component(name="espressif/esp_jpeg"' not in renderer_config
    assert 'add_lv_use("image", "label")' in renderer_config
    assert (
        'add_idf_component(name="espressif/esp_h264", ref="1.3.6")'
        in renderer_config
    )
    assert (
        'add_idf_component(name="espressif/esp_image_effects", ref="1.1.0")'
        in renderer_config
    )
    assert (
        'add_idf_component(name="espressif/esp_h264", ref="1.3.6")'
        in source_config
    )
    assert (
        'cg.add_build_flag("-Wl,--wrap=esp_h264_calloc_prefer")'
        in source_config
    )
    assert (
        'ref="50d258a34938014b5f43277573880d96bd8ed669"'
        in camera_config
    )
    assert (
        '"CONFIG_ESP_VIDEO_ENABLE_HW_JPEG_ENC_VIDEO_DEVICE", jpeg_enabled'
        in camera_config
    )
    h264_package = (
        ROOT / "packages" / "voip" / "p4_video_h264.yaml"
    ).read_text()
    assert (
        len(re.findall(r"(?m)^  task_stacks_in_psram: true$", h264_package)) == 2
    )
    assert "audio_task_stacks_in_psram: false" in h264_package
    assert "buffers_in_psram: false" in h264_package
    assert 'p4_video_width: "400"' in h264_package
    assert 'p4_video_height: "400"' in h264_package
    assert 'p4_video_fps: "10"' in h264_package
    assert 'p4_video_rx_width: "352"' in h264_package
    assert 'p4_video_rx_height: "288"' in h264_package
    assert 'p4_camera_fps: "25"' in h264_package
    assert 'p4_h264_gop: "10"' in h264_package
    assert "max_framerate: ${p4_camera_fps}" in h264_package
    assert "jpeg_new_decoder_engine" in renderer_cpp
    assert "jpeg_decoder_process" in renderer_cpp
    assert "ppa_do_scale_rotate_mirror" in renderer_cpp
    assert "this->direct_display_->draw_pixels_at(" in renderer_cpp
    assert "lv_image_create(container)" in renderer_cpp
    assert "lv_image_set_src(" in renderer_cpp
    assert "lv_img_create" not in renderer_cpp
    assert "lv_img_set_src" not in renderer_cpp
    assert "LV_EVENT_REFR_READY" in renderer_cpp
    assert "display_refresh_ready_callback_" in renderer_cpp
    assert "this->presentation_in_flight_.store(true" in renderer_cpp
    renderer_loop = renderer_cpp[
        renderer_cpp.index("void P4VideoRenderer::loop()") :
        renderer_cpp.index("void P4VideoRenderer::dump_config()")
    ]
    direct_display = renderer_loop[
        renderer_loop.index("#ifdef USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY") :
        renderer_loop.index("#else")
    ]
    assert direct_display.index("this->present_surface_direct_(pending)") < (
        direct_display.index("this->pending_surface_.compare_exchange_strong(")
    )
    assert "display_id: main_display" in h264_package
    assert "display_rotation: 270" in h264_package
    refresh_ready = renderer_cpp[
        renderer_cpp.index("void P4VideoRenderer::on_display_refresh_ready_()") :
        renderer_cpp.index(
            "voip_stack::VideoCapability\n"
            "P4VideoRenderer::get_receive_video_capability()"
        )
    ]
    assert "this->pending_surface_.load(std::memory_order_acquire) >= 0" in (
        refresh_ready
    )
    assert "this->enable_loop_soon_any_context();" in refresh_ready
    assert "vTaskDelay" not in renderer_cpp
    assert "MALLOC_CAP_SPIRAM" in renderer_cpp
    assert "xSemaphoreCreateMutexStatic(&this->presentation_mutex_storage_)" in (
        renderer_cpp
    )
    assert (
        "if (this->rx_running_.load(std::memory_order_acquire) &&"
        in renderer_cpp
    )
    assert "xSemaphoreTake(this->presentation_mutex_, portMAX_DELAY);" in renderer_cpp
    assert "StaticSemaphore_t presentation_mutex_storage_{};" in renderer_header
    assert "kH264Level30MaxMacroblocks = 1620" in renderer_header
    assert "h264_receive_profile_level_id_" in renderer_header
    assert "h264_optimized_yuv_bytes_() const" in renderer_header
    assert "kTaskStopTimeoutMs = 500" in renderer_header
    assert "const bool rotated_orientation" in renderer_cpp
    assert "!configured_orientation && !rotated_orientation" in renderer_cpp
    assert "macroblocks_w * macroblocks_h <=" in renderer_cpp
    assert '{396, 6000, "42c00c"}' in renderer_cpp
    assert "bilateral level asymmetry" in renderer_cpp
    assert "alloc_psram_dma(h264_yuv_bytes)" in renderer_cpp
    assert '#include "esp_imgfx_color_convert.h"' in renderer_header
    assert "ESP_IMGFX_PIXEL_FMT_I420" in renderer_cpp
    assert "ESP_IMGFX_PIXEL_FMT_O_UYY_E_VYY" in renderer_cpp
    assert "esp_imgfx_color_convert_process(" in renderer_cpp
    assert "i420_to_optimized_yuv420_" not in renderer_cpp
    assert "i420_to_optimized_yuv420_" not in renderer_header
    assert "input.raw_data.len -= input.consume;" in renderer_cpp
    assert "decoder_reset_pending_" in renderer_header
    assert "Failed to reset H.264 decoder for recovery IDR" in renderer_cpp
    assert "struct H264AccessUnitSlot" in renderer_header
    assert "uint32_t session_generation{0};" in renderer_header
    assert "uint32_t loss_generation{0};" in renderer_header
    assert "compare_exchange_strong(" in renderer_cpp
    assert 'add_idf_sdkconfig_option("CONFIG_ESP_H264_DECODER_IRAM", True)' in (
        renderer_config
    )
    assert 'add_idf_sdkconfig_option("CONFIG_ESP_H264_DUAL_TASK", False)' in (
        renderer_config
    )
    assert "CONFIG_ESP_H264_DUAL_TASK_CORE" not in (
        renderer_config
    )
    assert "CONFIG_ESP_H264_DUAL_TASK_PRIORITY" not in (
        renderer_config
    )
    h264_source_config = (
        ROOT
        / "esphome"
        / "components"
        / "esp_h264_video_source"
        / "__init__.py"
    ).read_text()
    assert "CONFIG_ESP_H264_DUAL_TASK" not in h264_source_config
    renderer_setup = renderer_cpp[
        renderer_cpp.index("void P4VideoRenderer::setup()") :
        renderer_cpp.index("void P4VideoRenderer::loop()")
    ]
    assert "this->configure_i420_converter_(this->width_, this->height_)" in (
        renderer_setup
    )
    assert "this->allocate_session_resources_()" in renderer_setup
    assert "this->mark_failed();" in renderer_setup
    renderer_start = renderer_cpp[
        renderer_cpp.index("bool P4VideoRenderer::start_video(") :
        renderer_cpp.index("bool P4VideoRenderer::set_video_active(")
    ]
    assert "this->allocate_session_resources_()" not in renderer_start
    assert "this->start_rx_task_()" not in renderer_start
    assert "void P4VideoRenderer::prepare_surface_(" in renderer_cpp
    render_i420_start = renderer_cpp.index(
        "bool P4VideoRenderer::render_i420_("
    )
    render_i420 = renderer_cpp[
        render_i420_start :
        renderer_cpp.index(
            "void P4VideoRenderer::prepare_surface_(",
            render_i420_start,
        )
    ]
    assert "esp_h264_dec_get_resolution(parameters, &resolution)" in renderer_cpp
    assert (
        "static_cast<uint16_t>(resolution.width),\n"
        "                                    static_cast<uint16_t>(resolution.height)"
        in renderer_cpp
    )
    assert (
        "std::min(static_cast<float>(kH264SurfaceWidth) / width,\n"
        "               static_cast<float>(kH264SurfaceHeight) / height)"
        in render_i420
    )
    assert "config.scale_x = scale;" in render_i420
    assert "config.scale_y = scale;" in render_i420
    assert "uint16_t surface_width = output_width;" in render_i420
    assert "uint16_t surface_height = output_height;" in render_i420
    assert "surface_width = output_height;" in render_i420
    assert "surface_height = output_width;" in render_i420
    assert "config.out.block_offset_x =" not in render_i420
    assert "config.out.block_offset_y =" not in render_i420
    assert "memset(this->surfaces_[output_index], 0, kSurfaceBytes)" not in (
        render_i420
    )
    direct_present = renderer_cpp[
        renderer_cpp.index("bool P4VideoRenderer::present_surface_direct_(") :
        renderer_cpp.index(
            "uint16_t P4VideoRenderer::jpeg_storage_width_",
            renderer_cpp.index(
                "bool P4VideoRenderer::present_surface_direct_("
            ),
        )
    ]
    assert "static black bars remain owned by" in direct_present
    assert "this->surface_content_width_[index]" in direct_present
    assert "this->surface_content_height_[index]" in direct_present
    assert "const int content_width = kH264SurfaceWidth;" not in (
        direct_present
    )
    stop_video = renderer_cpp[
        renderer_cpp.index("void P4VideoRenderer::stop_video()") :
        renderer_cpp.rindex("namespace esphome::p4_video_renderer")
    ]
    assert "this->rx_session_prepared_.store(false" in stop_video
    assert "this->stop_rx_task_();" not in stop_video
    assert "this->stop_rx_task_();" in renderer_setup
    assert "SIP teardown never joins it" in stop_video
    assert "free_codec_resources_();" not in stop_video
    assert "this->decoded_rgb565_" not in renderer_cpp
    jpeg_decode = renderer_cpp[
        renderer_cpp.index("jpeg_decoder_process(") :
        renderer_cpp.index("if (decode_error == ESP_OK")
    ]
    assert "this->surfaces_[output_index]" in jpeg_decode
    assert "class P4VideoRenderer" in renderer_header

    assert "class EspH264VideoSource" in source_header
    assert "public esp_video_camera::RawVideoFrameConsumer" in source_header
    assert "ESP_H264_RAW_FMT_O_UYY_E_VYY" in source_cpp
    assert "PPA_SRM_COLOR_MODE_YUV420" in source_cpp
    assert "RawVideoPixelFormat::YUV420_OUYY_EVYY" in source_cpp
    assert "V4L2_PIX_FMT_YUV420" in camera_cpp
    assert "capture_pixel_format_" in camera_cpp
    assert "h264_profile_level_id_from_annex_b" in source_cpp
    assert "h264_same_subprofile" in source_cpp
    assert "esp_h264_enc_set_bitrate" in source_cpp
    assert "extern \"C\" void *__wrap_esp_h264_calloc_prefer(" in source_cpp
    assert "bytes >= kLargeAllocationBytes" in source_cpp
    assert "caps1 == MALLOC_CAP_INTERNAL" in source_cpp
    assert "caps2 == MALLOC_CAP_SPIRAM" in source_cpp
    assert "n, size, actual_size, caps2, caps1" in source_cpp
    assert "capability.max_bitrate_bps" in source_cpp
    assert "ppa_unregister_client(this->ppa_)" in source_cpp
    source_setup = source_cpp[
        source_cpp.index("void EspH264VideoSource::setup()") :
        source_cpp.index("void EspH264VideoSource::on_shutdown()")
    ]
    assert "ppa_unregister_client(this->ppa_)" in source_setup
    consume_start = source_cpp.index(
        "void EspH264VideoSource::consume_raw_video_frame("
    )
    source_consume = source_cpp[
        consume_start : source_cpp.index("\n}  // namespace", consume_start)
    ]
    assert "this->init_ppa_()" in source_consume
    assert "Unable to register runtime H.264 PPA client" in source_consume
    assert "ppa_rotation_for_clockwise(frame.rotation_degrees)" in source_cpp
    assert "this->h264_optimized_yuv_bytes_()" in renderer_cpp
    assert "H.264 decode failure: error=%d" in renderer_cpp
    assert "ulTaskNotifyTake" not in source_cpp
    assert "vTaskDelay" not in source_cpp
    assert "request_key_frame()" in source_cpp
    assert "next_admit_timestamp_" in source_header
    assert "elapsed / minimum_delta + 1U" in source_cpp
    assert "static constexpr size_t kEncodedBufferBytes = 64 * 1024;" in (
        source_header
    )
    assert "uint8_t *tx_yuv_{nullptr};" in source_header
    assert "FrameSlot" not in source_header
    assert "frame_queue_" not in source_cpp
    assert "tx_task_" not in source_cpp
    assert source_consume.index("this->next_admit_timestamp_.store(") < (
        source_consume.index("this->transform_to_encoder_yuv_(")
    )
    assert "convert_avg_us=%u convert_max_us=%u" in source_cpp
    assert "encode_avg_us=%u encode_max_us=%u encoded_max_bytes=%u" in (
        source_cpp
    )
    request_key_frame = source_cpp[
        source_cpp.index("void EspH264VideoSource::request_key_frame()") :
        source_cpp.index("bool EspH264VideoSource::init_ppa_()")
    ]
    assert "xTaskNotifyGive" not in request_key_frame
    encode_frame = source_cpp[
        source_cpp.index("bool EspH264VideoSource::encode_frame_(") :
        source_cpp.index("void EspH264VideoSource::consume_raw_video_frame(")
    ]
    assert "xSemaphoreTake" not in encode_frame
    assert "this->tx_active_.load(std::memory_order_acquire)" in encode_frame
    assert "xSemaphoreTake(this->control_mutex_, portMAX_DELAY);" in source_consume
    assert "PPA_TRANS_MODE_BLOCKING" in source_cpp
    assert "PPA_TRANS_MODE_NON_BLOCKING" not in source_cpp
    assert "ppa_done_" not in source_header
    assert "pending_yuv_" not in source_header
    source_start = source_cpp[
        source_cpp.index("bool EspH264VideoSource::start_video(") :
        source_cpp.index("void EspH264VideoSource::stop_video()")
    ]
    assert "this->restart_encoder_()" not in source_start
    assert (
        "this->force_idr_generation_.store(\n"
        "      generation, std::memory_order_release);"
        in source_start
    )
    assert source_consume.index(
        "this->transform_to_encoder_yuv_("
    ) < source_consume.index("this->encode_frame_(")
    assert "generation ==" in encode_frame
    assert "this->tx_generation_.load(std::memory_order_acquire)" in encode_frame
    assert "start_tx_task_" not in source_setup
    source_stop = source_cpp[
        source_cpp.index("void EspH264VideoSource::stop_video()") :
        source_cpp.index("void EspH264VideoSource::request_key_frame()")
    ]
    assert "this->tx_active_.exchange(false" in source_stop
    assert "this->close_encoder_();" not in source_stop
    assert "ppa_unregister_client(this->ppa_)" not in source_stop
    assert "this->callback_(this->callback_ctx_, access_unit);" in encode_frame
    assert source_consume.index(
        "xSemaphoreTake(this->control_mutex_, portMAX_DELAY);"
    ) < source_consume.index("this->encode_frame_(") < source_consume.rindex(
        "xSemaphoreGive(this->control_mutex_);"
    )
    callback_revoke = source_stop.index("this->callback_ = nullptr;")
    assert callback_revoke < source_stop.index(
        "xSemaphoreGive(this->control_mutex_)"
    )
    assert "this->callback_ctx_ = nullptr;" in source_stop
    shutdown = source_cpp[
        source_cpp.index("void EspH264VideoSource::on_shutdown()") :
        source_cpp.index("void EspH264VideoSource::dump_config()")
    ]
    assert shutdown.index("this->stop_video();") < shutdown.index(
        "this->close_encoder_();"
    )
    assert "stop_tx_task_" not in shutdown
