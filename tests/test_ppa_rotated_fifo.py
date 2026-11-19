"""Exercise the exact official IDF decision, without a local PPA override."""

import hashlib
from pathlib import Path
import subprocess

import pytest

from tests.test_p4_ppa_backport import configure

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (
    ROOT / "tests/fixtures/espressif_p4_official/esp-idf"
    / "components/esp_driver_ppa/src/ppa_srm.c"
)
SOURCE_SHA = "6421c730681f5114fa4f614d6d14e87f066b57459bb51bb250b8546798fe13e8"


def fifo_workaround(source):
    return source.read_text().split("// Hardware bug workaround (DIG-734)", 1)[1].split(
        "ppa_ll_srm_bypass_mb_order(platform->hal.dev, bypass_mb_order);", 1
    )[0]


def test_fifo_workaround_follows_rotated_output_geometry(tmp_path):
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == SOURCE_SHA
    block = fifo_workaround(SOURCE)
    harness = r"""
#include <cstdint>
#include <cstdio>
constexpr int PPA_SRM_ROTATION_ANGLE_90=90,PPA_SRM_ROTATION_ANGLE_270=270;
constexpr int PPA_SRM_COLOR_MODE_ARGB8888=32,PPA_SRM_COLOR_MODE_RGB888=24;
constexpr unsigned PPA_LL_SRM_SCALING_FRAG_MAX=16,PPA_LL_SRM_MB_SIZE_16_16=16;
struct color_space_pixel_format_t {unsigned color_type_id;};
unsigned color_hal_pixel_format_get_bit_depth(color_space_pixel_format_t c){return c.color_type_id;}
struct Descriptor {struct {unsigned block_w,block_h;} in; unsigned scale_x_int,scale_x_frag,scale_y_int,scale_y_frag; int rotation_angle;};
bool shipped(unsigned w,unsigned h,unsigned sx,unsigned sy,int angle,unsigned depth,unsigned mb){
 Descriptor desc{{w,h},sx/16,sx%16,sy/16,sy%16,angle};auto*srm_trans_desc=&desc;
 unsigned mb_size=mb;unsigned ppa_out_color_mode=depth;
""" + block + r"""
 return bypass_mb_order;
}
// Independent physical-output oracle. Rotation acts on the scaled rectangle;
// the last DMA macroblock uses the corresponding original input axis.
bool physical(unsigned w,unsigned h,unsigned sx,unsigned sy,int angle,unsigned depth,unsigned mb){
 unsigned out_w=w*sx/16,source_rows=h,row_scale=sy;
 if(angle==90||angle==270){out_w=h*sy/16;source_rows=w;row_scale=sx;}
 const unsigned columns=depth==16?64:32;
 unsigned tail_columns=(out_w-1)%columns+1;
 unsigned tail_source_rows=(source_rows-1)%mb+1;
 unsigned tail_rows=tail_source_rows*row_scale/16;
 return (out_w>columns||source_rows>mb)&&tail_columns*tail_rows*depth<1536;
}
int main(){
 // The reported contract-admissible LVGL rectangle: upright does not need
 // bypass, a quarter turn does. This checks software geometry only: the
 // current display path uses a scratch buffer, not a scanned-out DPI buffer.
 if(shipped(424,134,16,16,0,16,16))return 1;
 if(!shipped(424,134,16,16,90,16,16))return 2;
 if(!shipped(424,134,16,16,270,16,16))return 3;
 // Official issue #19023's rotated witnesses and healthy control.
 for(auto dims:{std::pair{368u,66u},std::pair{364u,67u}})
  if(!shipped(dims.first,dims.second,16,16,270,16,16))return 4;
 if(shipped(600,40,16,16,270,16,16))return 5;
 // FIFO equality is not a qualified hardware fix. Preserve the official
 // strict boundary; neither this test nor issue #19023 proves <= necessary.
 if(shipped(96,3,16,16,0,16,16))return 6;
 for(unsigned w:{64u,66u,134u,424u,800u})for(unsigned h:{2u,16u,32u,134u,288u,800u})
 for(unsigned sx:{8u,13u,16u,17u,24u})for(unsigned sy:{8u,13u,16u,17u,24u})
 for(int angle:{0,90,180,270})for(unsigned depth:{16u,24u,32u})for(unsigned mb:{16u,32u}){
  if(shipped(w,h,sx,sy,angle,depth,mb)!=physical(w,h,sx,sy,angle,depth,mb)){
   std::fprintf(stderr,"geometry mismatch w=%u h=%u sx=%u sy=%u angle=%d depth=%u mb=%u\n",w,h,sx,sy,angle,depth,mb);return 5;
  }
 }
}
"""
    # Host headers for the geometry cases; the workaround block is unmodified.
    harness = "#include <initializer_list>\n#include <utility>\n" + harness
    fixture = tmp_path / "ppa.cpp"
    fixture.write_text(harness)
    executable = tmp_path / "ppa"
    subprocess.run(["c++", "-std=c++20", "-Wall", "-Wextra", "-Werror", "-O2",
                    str(fixture), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)


@pytest.mark.parametrize("version", ["5.5.6", "6.0.3"])
def test_hangup_redraw_on_stock_and_backported_driver(tmp_path, version):
    result, sdk, build, _ = configure(tmp_path, version=version)
    assert result.returncode == 0, result.stderr
    relative = Path("components/esp_driver_ppa/src/ppa_srm.c")
    for source, rotated_result in [
        (sdk / relative, (2304, 0)),
        (build / "p4_ppa_backport" / relative, (256, 1)),
    ]:
        # Keep the C decision intact. Supply only the register and format inputs.
        harness = r"""
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

enum { PPA_SRM_ROTATION_ANGLE_90 = 90, PPA_SRM_ROTATION_ANGLE_270 = 270,
       PPA_SRM_COLOR_MODE_RGB565, PPA_SRM_COLOR_MODE_RGB888, PPA_SRM_COLOR_MODE_ARGB8888,
       PPA_LL_SRM_MB_SIZE_16_16 = 16, PPA_LL_SRM_SCALING_FRAG_MAX = 16 };
typedef struct { uint32_t color_type_id; } color_space_pixel_format_t;
typedef uint32_t esp_color_fourcc_t;
#define color_hal_pixel_format_get_bit_depth(format) ((format).color_type_id == PPA_SRM_COLOR_MODE_RGB565 ? 16u : 0u)
#define color_hal_pixel_format_fourcc_get_bit_depth(format) ((format) == PPA_SRM_COLOR_MODE_RGB565 ? 16u : 0u)
#define ppa_ll_srm_get_mb_size(dev) (mb)

int main(int argc, char **argv) {
    if (argc != 5) return 1;
    struct descriptor {
        struct { uint32_t block_w, block_h; } in;
        uint32_t scale_x_int, scale_x_frag, scale_y_int, scale_y_frag;
        int rotation_angle;
    } desc = {{atoi(argv[1]), atoi(argv[2])}, 1, 0, 1, 0, atoi(argv[3])};
    const struct descriptor *srm_trans_desc = &desc;
    const int mb = atoi(argv[4]);
    const uint32_t ppa_out_color_mode = PPA_SRM_COLOR_MODE_RGB565;
""" + fifo_workaround(source) + r"""
    printf("%u %d\n", w_left * h_left * out_pixel_depth, bypass_mb_order);
    return 0;
}
"""
        fixture = tmp_path / "redraw.c"
        executable = tmp_path / "redraw"
        fixture.write_text(harness)
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", str(fixture),
                        "-o", str(executable)], check=True)

        for mb in (16, 32):
            for width, height, angle, expected in [
                (228, 68, 90, rotated_result),   # Hangup button redraw in the core dump.
                (228, 68, 0, (2304, 0)),         # Same rectangle without rotation.
                (800, 800, 90, (32 * mb * 16, 0)),  # Concurrent RGB565 camera frame.
            ]:
                output = subprocess.check_output(
                    [str(executable), str(width), str(height), str(angle), str(mb)], text=True
                )
                assert tuple(map(int, output.split())) == expected, (source, width, height, angle, mb)
