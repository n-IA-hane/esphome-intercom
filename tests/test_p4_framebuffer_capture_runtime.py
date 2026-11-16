"""The raw diagnostic consumes an owner-controlled public frame copy."""

from pathlib import Path
import json
import resource
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "esphome/components/p4_framebuffer_capture/p4_framebuffer_capture.cpp"


def _no_core_dump():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


@pytest.mark.parametrize("sync_error", [False, True])
def test_snapshot_reads_dma_memory_and_does_not_write_back_stale_cache(
    tmp_path, sync_error
):
    headers = {
        "esphome/core/component.h": "#pragma once\nnamespace esphome {class Component {public:virtual void setup(){};void mark_failed(){}};}\n",
        "esphome/core/log.h": "#pragma once\n#define ESP_LOGE(...)\n#define ESP_LOGW(...)\n#define ESP_LOGI(...)\n",
        "esphome/components/mipi_dsi/mipi_dsi.h": """#pragma once
#include <cstddef>
#include <cstdint>
namespace esphome::mipi_dsi {struct MipiDsi {
 uint16_t memory[16]{};bool available=true;unsigned copies=0;
 int get_width(){return 4;}int get_height(){return 4;}
 bool copy_frame_buffer_rgb565(uint8_t *target,size_t capacity){
  ++copies;if(!available||capacity<32)return false;
  for(unsigned i=0;i<16;++i){target[2*i]=memory[i]&255;target[2*i+1]=memory[i]>>8;}
  return true;
 }
};}
""",
        "esp_heap_caps.h": "#pragma once\n#include <cstdlib>\n#define MALLOC_CAP_SPIRAM 1\n#define MALLOC_CAP_8BIT 2\ninline void*heap_caps_malloc(size_t n,int){return std::calloc(1,n);}\n",
        "lwip/inet.h": "#include <arpa/inet.h>\n",
        "lwip/sockets.h": "#include <sys/socket.h>\n#include <unistd.h>\n",
        "freertos/FreeRTOS.h": "#pragma once\n#define pdPASS 1\n",
        "freertos/task.h": "#pragma once\nint xTaskCreate(void(*)(void*),const char*,unsigned,void*,unsigned,void*);\ninline void vTaskDelete(void*){}\n",
    }
    for name, text in headers.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (tmp_path / "main.cpp").write_text(f"""
#include <cassert>
#include <cstdlib>
#include "{SOURCE.parent / "p4_framebuffer_capture.h"}"
static int task_calls=0;
int xTaskCreate(void(*)(void*),const char*,unsigned,void*,unsigned,void*){{++task_calls;return 1;}}
struct Probe:esphome::p4_framebuffer_capture::P4FramebufferCapture{{
 using P4FramebufferCapture::snapshot_;using P4FramebufferCapture::capture_active_;
 using P4FramebufferCapture::snapshot_capacity_;using P4FramebufferCapture::snapshot_size_;
 using P4FramebufferCapture::snapshot_width_;using P4FramebufferCapture::snapshot_height_;
}};
int main(){{
 for(unsigned factor:{{1u,2u,4u}}){{
  task_calls=0;
  esphome::mipi_dsi::MipiDsi display;display.available=!{int(sync_error)};
  for(unsigned i=0;i<16;++i)display.memory[i]=0x1234+i;
  Probe probe;probe.set_display(&display);probe.set_downsample(factor);probe.setup();
  assert(probe.snapshot_capacity_==32);probe.capture();
  assert(display.copies==1);
  if({int(sync_error)}){{assert(task_calls==0&&!probe.capture_active_.load());}}
  else{{
   unsigned width=4/factor;
   assert(probe.snapshot_width_==int(width)&&probe.snapshot_height_==int(width));
   assert(probe.snapshot_size_==width*width*2);
   for(unsigned y=0;y<width;++y)for(unsigned x=0;x<width;++x){{
    uint16_t expected=0x1234+(y*factor)*4+x*factor;unsigned offset=(y*width+x)*2;
    assert(probe.snapshot_[offset]==(expected&255)&&probe.snapshot_[offset+1]==(expected>>8));
   }}
   assert(task_calls==1);
  }}
  for(unsigned i=0;i<16;++i)assert(display.memory[i]==0x1234+i);
  std::free(probe.snapshot_);
 }}
}}
""")
    binary = tmp_path / "probe"
    compiled = subprocess.run(
        [
            "g++",
            "-std=c++17",
            "-I",
            str(tmp_path),
            str(SOURCE),
            str(tmp_path / "main.cpp"),
            "-o",
            str(binary),
        ],
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    subprocess.run([str(binary)], check=True, preexec_fn=_no_core_dump)


@pytest.mark.parametrize(
    ("provider", "display_depths", "target", "accepted"),
    [
        ("owned", [16], "screen_0", True),
        ("owned", [24], "screen_0", False),
        ("owned", [16, 24], "screen_1", False),
        ("stock", [16], "screen_0", False),
    ],
    ids=["owned-rgb565", "rgb888", "other-display-capable", "stock-no-readback"],
)
def test_optional_diagnostic_uses_actual_provider_schema(
    tmp_path, provider, display_depths, target, accepted
):
    config = {
        "esphome": {"name": "raw-capture-schema", "build_path": str(tmp_path / "build")},
        "esp32": {
            "board": "esp32-p4-evboard", "variant": "esp32p4",
            "framework": {"type": "esp-idf"},
        },
        "psram": {"mode": "hex", "speed": "200MHz"},
        "external_components": [{
            "source": {"type": "local", "path": str(ROOT / "esphome/components")},
            "components": (["mipi_dsi"] if provider == "owned" else [])
            + ["p4_framebuffer_capture"],
        }],
        "esp_ldo": [{"channel": 3, "voltage": "2.5V"}],
        "display": [{
            "platform": "mipi_dsi", "id": f"screen_{index}",
            "model": "WAVESHARE-10.1-DSI-TOUCH-A",
            "reset_pin": 27 - index, "color_depth": f"{depth}bit",
            "dimensions": {"width": 800, "height": 1280},
            "auto_clear_enabled": False, "update_interval": "never",
        } for index, depth in enumerate(display_depths)],
        "p4_framebuffer_capture": {
            "id": "raw_capture", "display_id": target, "host": "192.0.2.1",
        },
    }
    path = tmp_path / "schema.yaml"
    path.write_text(json.dumps(config))
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "config", str(path)],
        capture_output=True, text=True, timeout=90,
    )
    output = result.stdout + result.stderr
    if accepted:
        assert result.returncode == 0, output
    else:
        assert result.returncode != 0, output
        assert "copy_frame_buffer_rgb565" in output, output
