# Experimental microWakeWord task affinity

Based on ESPHome 2026.9.0, tag commit
`c6e4c87e525dd343e470d8dea368957a002f6505`. ESPHome licensing and authorship
apply to the copied source files.

This fork adds `task_core: -1 | 0 | 1`. The default `-1` keeps the upstream
unpinned task creation behavior. Core 1 is rejected on single-core targets.
Choosing a core does not change the model, detection thresholds, audio format,
task priority, stack placement or task cleanup.

On ESP32-S3, FreeRTOS pins a previously unpinned task when it executes floating
point instructions. Explicit affinity lets a constrained full audio profile
keep wake word inference separate from AFE feed processing across restarts.
It does not guarantee adequate processing capacity; real audio qualification
is still required.

`inference_task.h/.cpp` preserve ESPHome 2026.9.0 StaticTask's allocation and
cleanup, with a namespace-local name and an optional core argument passed to
`xTaskCreateStaticPinnedToCore`. This temporary local helper avoids modifying
the installed ESPHome core. A future upstream proposal should extend the shared
StaticTask helper instead of keeping this copy.

Only YAMLs that explicitly select this external component use the fork.
The Waveshare V2 candidate is experimental; no upstream PR has been submitted.
