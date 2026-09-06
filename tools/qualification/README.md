# Isolated OTA qualification patch

`ota-progress-2026.8.2.patch` preserves the OTA change tested in the P4
2026.9.1 candidate. It applies to ESPHome 2026.8.2's
`esphome/components/ota/ota_backend_esp_idf.cpp`, SHA256
`5fd696207547bd25007f38bc721a4fd6a09355af018dfbb77ff8ccc3a56f8efb`.

The two complete image-verification passes could exceed the watchdog budget
together. The patch feeds the watchdog at completed-operation boundaries and
logs each duration. It preserves image verification, errors and the normal
watchdog timeout. It does not change the companion C6 firmware or ESP-IDF.

The tested builds use a disposable copy of the installed `ota` component,
with this patch applied, selected through `external_components`. Apply the
patch inside that copy and retain its original source hash. Do not patch the
global ESPHome package or assume the maintained remote YAML enables it.

This is qualification evidence pending integration with the normal dependency
workflow. The prebuilt P4 candidate contains the patch; ordinary builds from
the maintained YAML do not yet include it automatically.
