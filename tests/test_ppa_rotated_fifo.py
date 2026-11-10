"""Compile the shipped DIG-734 decision and check physical rotated geometry."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "idf_components/esp_driver_ppa/src/ppa_srm.c"


def test_fifo_workaround_follows_rotated_output_geometry(tmp_path):
    source = SOURCE.read_text()
    block = source.split("// Hardware bug workaround (DIG-734)", 1)[1].split(
        "ppa_ll_srm_bypass_mb_order(platform->hal.dev, bypass_mb_order);", 1
    )[0]
    harness = r"""
#include <cstdint>
#include <cstdio>
constexpr int PPA_SRM_ROTATION_ANGLE_90=90,PPA_SRM_ROTATION_ANGLE_270=270;
constexpr int PPA_SRM_COLOR_MODE_ARGB8888=32,PPA_SRM_COLOR_MODE_RGB888=24;
constexpr unsigned PPA_LL_SRM_SCALING_FRAG_MAX=16,PPA_LL_SRM_MB_SIZE_16_16=16;
struct color_space_pixel_format_t {unsigned color_type_id;};
unsigned color_hal_pixel_format_get_bit_depth(color_space_pixel_format_t c){return c.color_type_id;}
unsigned ppa_ll_srm_get_mb_size(unsigned mb){return mb;}
struct Descriptor {struct {unsigned block_w,block_h;} in; unsigned scale_x_int,scale_x_frag,scale_y_int,scale_y_frag; int rotation_angle;};
struct Platform {struct {unsigned dev;} hal;};
bool shipped(unsigned w,unsigned h,unsigned sx,unsigned sy,int angle,unsigned depth,unsigned mb){
 Descriptor desc{{w,h},sx/16,sx%16,sy/16,sy%16,angle};auto*srm_trans_desc=&desc;
 Platform owner{{mb}};auto*platform=&owner;unsigned ppa_out_color_mode=depth;
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
 return (out_w>columns||source_rows>mb)&&tail_columns*tail_rows*depth<=1536;
}
int main(){
 // The reported contract-admissible LVGL rectangle: upright does not need
 // bypass, a quarter turn does. Equality repair from stable remains intact.
 if(shipped(424,134,16,16,0,16,16))return 1;
 if(!shipped(424,134,16,16,90,16,16))return 2;
 if(!shipped(424,134,16,16,270,16,16))return 3;
 if(!shipped(96,3,16,16,0,16,16))return 4;
 for(unsigned w:{64u,66u,134u,424u,800u})for(unsigned h:{2u,16u,32u,134u,288u,800u})
 for(unsigned sx:{8u,13u,16u,17u,24u})for(unsigned sy:{8u,13u,16u,17u,24u})
 for(int angle:{0,90,180,270})for(unsigned depth:{16u,24u,32u})for(unsigned mb:{16u,32u}){
  if(shipped(w,h,sx,sy,angle,depth,mb)!=physical(w,h,sx,sy,angle,depth,mb)){
   std::fprintf(stderr,"geometry mismatch w=%u h=%u sx=%u sy=%u angle=%d depth=%u mb=%u\n",w,h,sx,sy,angle,depth,mb);return 5;
  }
 }
}
"""
    # Range-for initializer lists require only this host standard header.
    harness = "#include <initializer_list>\n" + harness
    fixture = tmp_path / "ppa.cpp"
    fixture.write_text(harness)
    executable = tmp_path / "ppa"
    subprocess.run(["c++", "-std=c++20", "-Wall", "-Wextra", "-Werror", "-O2",
                    str(fixture), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
