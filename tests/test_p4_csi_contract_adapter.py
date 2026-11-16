"""Exercise the exact official CSI version boundary without source rewriting."""

import hashlib
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (
    ROOT / "tests/fixtures/espressif_p4_official/esp-video-components"
    / "esp_video/src/device/esp_video_csi_format.c"
)
SOURCE_SHA = "9f7cb1453031448b7f11094a6c5093d92fa3866754b42b543a0bca5e9cc946eb"


@pytest.mark.parametrize("version", [(5, 5, 5), (5, 5, 6), (5, 5, 7), (6, 0, 0)])
@pytest.mark.parametrize("converts", [False, True])
def test_official_csi_input_matches_isp_output_from_556(tmp_path, version, converts):
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == SOURCE_SHA
    source = SOURCE.read_text()
    # Retain both real branches of the RAW->ISP->CSI path. Stub only color
    # conversions and diagnostics; the version test and assignments are vendor C.
    start = source.index("#if ESP_VIDEO_CSI_DEVICE_CONV_FORMAT", source.index(
        "/* ISP can process the data (sensor outputs RAW format) */"
    ))
    block = source[start:source.index(
        "#endif /* ESP_VIDEO_CSI_DEVICE_CONV_FORMAT */", start
    ) + len("#endif /* ESP_VIDEO_CSI_DEVICE_CONV_FORMAT */")]
    expected = 116 if converts or version >= (5, 5, 6) else 108
    encoded = version[0] * 65536 + version[1] * 256 + version[2]
    harness = f"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#define ESP_IDF_VERSION_VAL(a,b,c) ((a)*65536+(b)*256+(c))
#define ESP_IDF_VERSION {encoded}
#define ESP_VIDEO_CSI_DEVICE_CONV_FORMAT {int(converts)}
#define ESP_OK 0
#define ESP_ERR_NOT_SUPPORTED -1
#define ESP_LOGD(...) ((void)0)
#define ISP_COLOR_RGB888 224
#define V4L2_PIX_FMT_RGB24 24
struct Formats {{ unsigned isp_output_fmt, csi_input_fmt, csi_output_fmt; }};
static unsigned v4l2_to_isp_color(unsigned value) {{ return value + 200; }}
static unsigned v4l2_to_csi_color(unsigned value) {{ return value + 100; }}
#if ESP_VIDEO_CSI_DEVICE_CONV_FORMAT
static bool is_csi_supported_input(unsigned value) {{ return value == 16 || value == 24; }}
#endif
static int check(struct Formats *in_out_format, bool isp_can_output) {{
 const uint32_t sensor_v4l2_fmt = 8, requested_fmt = 16;
 (void)sensor_v4l2_fmt;
{block}
}}
int main(void) {{
 struct Formats result = {{0}};
 assert(check(&result, true) == ESP_OK);
 assert(result.isp_output_fmt == 216);
 assert(result.csi_input_fmt == {expected});
 assert(result.csi_output_fmt == 116);
#if ESP_VIDEO_CSI_DEVICE_CONV_FORMAT
 assert(check(&result, false) == ESP_OK);
 assert(result.isp_output_fmt == ISP_COLOR_RGB888);
 assert(result.csi_input_fmt == 124);
 assert(result.csi_output_fmt == 116);
#else
 assert(check(&result, false) == ESP_ERR_NOT_SUPPORTED);
#endif
 return 0;
}}
"""
    fixture = tmp_path / "csi.c"
    fixture.write_text(harness)
    executable = tmp_path / "csi"
    subprocess.run(
        ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", str(fixture), "-o", str(executable)],
        check=True, capture_output=True,
    )
    subprocess.run([str(executable)], check=True, capture_output=True)
