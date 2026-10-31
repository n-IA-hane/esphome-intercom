"""Exercise the maintained MIPI DSI framebuffer provider used by screenshots."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_p4_snapshot_serializes_dma_and_converts_rotation_and_byte_order(tmp_path):
    driver = ROOT / "esphome/components/mipi_dsi"
    implementation = (driver / "mipi_dsi.cpp").read_text()
    method = implementation.split("bool MipiDsi::copy_frame_buffer_rgb565(", 1)[
        1
    ].split("\n#endif", 1)[0]
    method = "bool MipiDsi::copy_frame_buffer_rgb565(" + method
    header = (driver / "mipi_dsi.h").read_text()
    rotate = (
        "  void rotate_("
        + header.split("  void rotate_(", 1)[1].split("\n  void mark_dirty_", 1)[0]
    )
    harness = tmp_path / "p4.cpp"
    harness.write_text(
        r"""
#include <array>
#include <cassert>
#include <cstdint>
#include <cstddef>
#include <utility>
#include <vector>
static bool locked=false, sync_failure=false;
static int syncs=0;
constexpr int pdTRUE=1, ESP_OK=0, ESP_CACHE_MSYNC_FLAG_DIR_M2C=8;
int xSemaphoreTake(void*,int ticks){assert(ticks==0);if(locked)return 0;locked=true;return 1;}
void xSemaphoreGive(void*){assert(locked);locked=false;}
int esp_cache_msync(void*,size_t size,int flags){assert(locked&&size==12&&flags==8);syncs++;return sync_failure?1:0;}
uint16_t convert_big_endian(uint16_t v){return uint16_t((v>>8)|(v<<8));}
namespace display {
enum Rotation{DISPLAY_ROTATION_0_DEGREES=0,DISPLAY_ROTATION_90_DEGREES=90,
 DISPLAY_ROTATION_180_DEGREES=180,DISPLAY_ROTATION_270_DEGREES=270};
using DisplayRotation=Rotation;
constexpr int COLOR_BITNESS_565=16;
}
struct MipiDsi {
 int color_depth_=16; bool big_endian_=false,failed_=false; size_t width_=3,height_=2;
 uint8_t *frame_buffer_=nullptr;size_t frame_buffer_size_=12;
 void *submit_mutex_=this;int rotation_=0;
 bool is_failed(){return failed_;}
 int get_width(){return rotation_==90||rotation_==270?2:3;}
 int get_height(){return rotation_==90||rotation_==270?3:2;}
 bool copy_frame_buffer_rgb565(uint8_t*,size_t,display::Rotation=display::DISPLAY_ROTATION_0_DEGREES);
"""
        + rotate
        + "\n};\n"
        + method
        + r"""
using Matrix=std::vector<std::vector<uint16_t>>;
Matrix clockwise(Matrix input,unsigned turns){
 while(turns--){
  Matrix next(input[0].size(),std::vector<uint16_t>(input.size()));
  for(size_t y=0;y<input.size();++y)
   for(size_t x=0;x<input[0].size();++x)next[x][input.size()-1-y]=input[y][x];
  input=next;
 }
 return input;
}
int main(){
 MipiDsi d;std::array<uint16_t,6> input{1,2,3,4,5,6};
 std::array<uint16_t,8> output{};
 d.frame_buffer_=reinterpret_cast<uint8_t*>(input.data());
 auto *target=reinterpret_cast<uint8_t*>(output.data());
 const Matrix native{{1,2,3},{4,5,6}};
 for(int driver:{0,90,180,270})for(int rotation:{0,90,180,270})for(bool big:{false,true}){
  d.rotation_=driver;d.big_endian_=big;
  for(size_t i=0;i<input.size();++i)input[i]=big?convert_big_endian(i+1):i+1;
  const auto immutable=input;output.fill(0xaaaa);
  const auto logical=clockwise(native,(360-driver)%360/90);
  const auto expected=clockwise(logical,rotation/90);
  const auto previous_syncs=syncs;
  assert(d.copy_frame_buffer_rgb565(target,sizeof(output),static_cast<display::Rotation>(rotation)));
  size_t index=0;for(const auto &row:expected)for(auto pixel:row)assert(output[index++]==pixel);
  assert(index==input.size()&&output[6]==0xaaaa&&output[7]==0xaaaa);
  assert(input==immutable&&!locked&&syncs==previous_syncs+1);
  // Existing two-argument callers retain the driver's logical orientation.
  assert(d.copy_frame_buffer_rgb565(target,sizeof(input)));
  index=0;for(const auto &row:logical)for(auto pixel:row)assert(output[index++]==pixel);
 }
 d.rotation_=0;d.big_endian_=false;input={1,2,3,4,5,6};
 output.fill(0xbbbb);const auto before=output;const auto immutable=input;const int previous_syncs=syncs;
 assert(!d.copy_frame_buffer_rgb565(target,sizeof(input)-1,display::DISPLAY_ROTATION_90_DEGREES));
 assert(!d.copy_frame_buffer_rgb565(nullptr,sizeof(input)));
 assert(!d.copy_frame_buffer_rgb565(target,sizeof(input),static_cast<display::Rotation>(45)));
 d.frame_buffer_size_=10;assert(!d.copy_frame_buffer_rgb565(target,sizeof(input)));d.frame_buffer_size_=12;
 d.frame_buffer_=nullptr;assert(!d.copy_frame_buffer_rgb565(target,sizeof(input)));d.frame_buffer_=reinterpret_cast<uint8_t*>(input.data());
 d.submit_mutex_=nullptr;assert(!d.copy_frame_buffer_rgb565(target,sizeof(input)));d.submit_mutex_=&d;
 d.failed_=true;assert(!d.copy_frame_buffer_rgb565(target,sizeof(input)));d.failed_=false;
 d.color_depth_=24;assert(!d.copy_frame_buffer_rgb565(target,sizeof(input)));d.color_depth_=16;
 locked=true;assert(!d.copy_frame_buffer_rgb565(target,sizeof(input)));
 assert(output==before&&input==immutable&&syncs==previous_syncs);
 locked=false;sync_failure=true;
 assert(!d.copy_frame_buffer_rgb565(target,sizeof(input),display::DISPLAY_ROTATION_270_DEGREES));
 assert(output==before&&input==immutable&&!locked&&syncs==previous_syncs+1);
}
"""
    )
    binary = tmp_path / "p4"
    subprocess.run(
        [
            "g++",
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fsanitize=address,undefined",
            "-fno-pie",
            "-no-pie",
            str(harness),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)
