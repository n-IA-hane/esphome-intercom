# esp_video 2.3.0 declares esp_h264 unconditionally for ESP32-P4 even though
# its own sources and CMake dependency are gated by
# CONFIG_ESP_VIDEO_ENABLE_H264_VIDEO_DEVICE. A local empty component lets the
# JPEG-only profile satisfy that inactive manifest edge without downloading or
# compiling the H.264 implementation. The H.264 camera profile does not copy
# this stub and therefore resolves the real managed component.
idf_component_register()
