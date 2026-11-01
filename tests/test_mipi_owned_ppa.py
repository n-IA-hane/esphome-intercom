"""Exercise production framebuffer ownership and transforms at the PPA boundary."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "esphome/components/mipi_dsi"


def method(source, name):
    prefix = "bool" if "bool MipiDsi::" + name + "(" in source else "void"
    start = source.index(prefix + " MipiDsi::" + name + "(")
    return source[start:source.index("\n}", start) + 2]


def test_owned_target_preserves_pixels_bounds_and_completion_lifetimes(tmp_path):
    source = (DRIVER / "mipi_dsi.cpp").read_text()
    header = (DRIVER / "mipi_dsi.h").read_text()
    methods = "\n".join(method(source, name) for name in (
        "supports_native_rgb565", "supports_rgb565_transform", "draw_native_rgb565", "mirror_native_rgb565_",
        "draw_rgb565_transformed", "draw_rotated_rgb565_", "draw_pixels_at", "draw_pixels_compat_",
        "draw_pixel_at", "mark_dirty_", "flush_dirty_", "write_to_display_", "check_buffer_",
        "submit_bitmap_", "submit_bitmap_locked_", "copy_frame_buffer_rgb565",
    ))
    rotate = "  void rotate_(" + header.split("  void rotate_(", 1)[1].split("\n  void mark_dirty_", 1)[0]
    code = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <future>
#include <mutex>
#include <utility>
#include <vector>
using namespace std::chrono_literals;
constexpr int ESP_OK=0,portMAX_DELAY=-1,pdTRUE=1,PPA_TRANS_MODE_BLOCKING=1;
constexpr int PPA_SRM_COLOR_MODE_RGB565=16,ESP_CACHE_MSYNC_FLAG_DIR_M2C=8,ESP_CACHE_MSYNC_FLAG_UNALIGNED=1;
template<typename T>struct RAMAllocator{T*allocate(size_t n){return static_cast<T*>(std::calloc(n,sizeof(T)));}};
enum ppa_srm_rotation_angle_t {PPA_SRM_ROTATION_ANGLE_0,PPA_SRM_ROTATION_ANGLE_90,PPA_SRM_ROTATION_ANGLE_180,PPA_SRM_ROTATION_ANGLE_270};
struct ppa_pic {const void*buffer{};uint32_t pic_w{},pic_h{},block_w{},block_h{},block_offset_x{},block_offset_y{};int srm_cm{};};
struct ppa_out {void*buffer{};uint32_t buffer_size{},pic_w{},pic_h{},block_offset_x{},block_offset_y{};int srm_cm{};};
struct ppa_srm_oper_config_t {ppa_pic in;ppa_out out;ppa_srm_rotation_angle_t rotation_angle{};float scale_x{},scale_y{};int mode{};void*user_data{};};
struct Semaphore {std::mutex lock;std::condition_variable changed;std::atomic<bool>available;explicit Semaphore(bool v):available(v){}};
bool xSemaphoreTake(Semaphore*s,int wait){std::unique_lock lock(s->lock);if(!wait&&!s->available)return false;s->changed.wait(lock,[&]{return bool(s->available);});s->available=false;return true;}
void xSemaphoreGive(Semaphore*s){{std::lock_guard lock(s->lock);s->available=true;}s->changed.notify_all();}
struct Color {uint16_t value;uint8_t r{},g{},b{};};
namespace display {
enum DisplayRotation{DISPLAY_ROTATION_0_DEGREES=0,DISPLAY_ROTATION_90_DEGREES=90,DISPLAY_ROTATION_180_DEGREES=180,DISPLAY_ROTATION_270_DEGREES=270};
using ColorBitness=int;using ColorOrder=int;constexpr int COLOR_BITNESS_888=0,COLOR_BITNESS_565=1,COLOR_BITNESS_332=2,COLOR_ORDER_RGB=0,COLOR_ORDER_BGR=1;
struct ColorUtil{static uint16_t color_to_565(Color c){return c.value;}};
struct Display {unsigned legacy_calls=0;virtual ~Display()=default;virtual void draw_pixel_at(int,int,Color)=0;void draw_pixels_at(int,int,int,int,const uint8_t*,int,int,bool,int,int,int);};
}
struct MipiDsi;
struct Driver {MipiDsi*owner{};std::atomic<unsigned>submits{0},callbacks{0},copies{0};bool reject=false,defer=false;Semaphore entered{false};};
using esp_err_t=int;
int esp_lcd_panel_draw_bitmap(Driver*,int,int,int,int,const uint8_t*);
int ppa_do_scale_rotate_mirror(void*,const ppa_srm_oper_config_t*);
int esp_cache_msync(void*,size_t,int);
const char*esp_err_to_name(int){return "injected";}
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGV(...) ((void)0)
#define LOG_STR(x) x
uint16_t convert_big_endian(uint16_t x){return uint16_t((x<<8)|(x>>8));}
struct MipiDsi:display::Display {
 size_t width_=19,height_=17;int color_depth_=1;bool big_endian_=false,failed=false;
 std::vector<uint16_t>pixels=std::vector<uint16_t>(width_*height_,0xaaaa),shadow;
 uint8_t*frame_buffer_=reinterpret_cast<uint8_t*>(pixels.data());size_t frame_buffer_size_=pixels.size()*2;
 uint8_t*buffer_=nullptr;void*transform_client_=this;
 Driver driver{this};Driver*handle_=&driver;Semaphore owner{true},completion{true};
 Semaphore*submit_mutex_=&owner,*io_lock_=&completion;int rotation_=0;
 std::atomic<unsigned>ppa_calls{0};bool reject_ppa=false;std::function<void()>ppa_hook;
 bool is_failed()const{return failed;}void smark_failed(const char*,int){failed=true;}
 int get_width(){return rotation_==90||rotation_==270?height_:width_;}
 int get_height(){return rotation_==90||rotation_==270?width_:height_;}
 using WriteAdmission=bool(*)(void*);
 bool supports_native_rgb565()const;
 bool supports_rgb565_transform()const;
 bool draw_native_rgb565(int,int,int,int,const uint8_t*,size_t);
 void mirror_native_rgb565_(int,int,int,int,const uint8_t*,size_t);
 bool clipping=false,updating_=false;uint16_t x_low_=1,y_low_=1,x_high_=0,y_high_=0;
 struct Clip{bool inside(int x,int y)const{return x>=2&&y>=2&&x<8&&y<8;}};
 std::vector<Clip>clipping_rectangle_{Clip{}};
 bool is_clipping()const{return clipping;}
 int get_width_internal(){return width_;}int get_height_internal(){return height_;}
 bool check_buffer_();
 ~MipiDsi(){if(buffer_&&buffer_!=reinterpret_cast<uint8_t*>(shadow.data()))std::free(buffer_);}
 void mark_failed(const char*){failed=true;}
 uint16_t to_panel_565_(Color c)const{return big_endian_?convert_big_endian(c.value):c.value;}
 void draw_pixels_at(int,int,int,int,const uint8_t*,int,int,bool,int,int,int);
 void draw_pixels_compat_(int,int,int,int,const uint8_t*,int,bool,int,int,int);
 void draw_pixel_at(int,int,Color)override;
 void mark_dirty_(int,int,int,int);void flush_dirty_();
 void write_to_display_(int,int,int,int,const uint8_t*,int,int,int);
 bool draw_rgb565_transformed(const ppa_srm_oper_config_t&,size_t,int,int,int,int,WriteAdmission=nullptr,void*context=nullptr);
 bool draw_rotated_rgb565_(int,int,int,int,const uint8_t*,int,int,int);
 bool submit_bitmap_(int,int,int,int,const uint8_t*);
 bool submit_bitmap_locked_(int,int,int,int,const uint8_t*);
 bool copy_frame_buffer_rgb565(uint8_t*,size_t,display::DisplayRotation=display::DISPLAY_ROTATION_0_DEGREES);
''' + rotate + r'''
 };
void display::Display::draw_pixels_at(int x,int y,int width,int height,const uint8_t*data,int order,int bitness,bool big,int xo,int yo,int pad){
 ++legacy_calls;assert(bitness==display::COLOR_BITNESS_565);auto*p=reinterpret_cast<const uint16_t*>(data);int stride=xo+width+pad;
 for(int row=0;row<height;row++)for(int col=0;col<width;col++){uint16_t value=p[(row+yo)*stride+col+xo];if(big)value=convert_big_endian(value);if(order==display::COLOR_ORDER_BGR)value=uint16_t((value&0x7e0)|((value&31)<<11)|((value>>11)&31));draw_pixel_at(x+col,y+row,Color{value});}
}
int esp_cache_msync(void*ptr,size_t bytes,int flags){(void)ptr;assert(bytes&&(flags&8)==8);return 0;}
int esp_lcd_panel_draw_bitmap(Driver*d,int x,int y,int end_x,int end_y,const uint8_t*ptr){
 assert(!d->owner->owner.available);++d->submits;xSemaphoreGive(&d->entered);
 assert(x>=0&&y>=0&&end_x<=int(d->owner->width_)&&end_y<=int(d->owner->height_));
 auto begin=reinterpret_cast<uintptr_t>(d->owner->frame_buffer_),value=reinterpret_cast<uintptr_t>(ptr);
 if(!(value>=begin&&value<begin+d->owner->frame_buffer_size_)){
  if(!d->reject){for(int row=y;row<end_y;row++)std::memcpy(d->owner->frame_buffer_+(row*d->owner->width_+x)*2,ptr+(row-y)*(end_x-x)*2,(end_x-x)*2);++d->copies;}
 }
 if(d->reject)return 1;
 if(!d->defer){++d->callbacks;xSemaphoreGive(&d->owner->completion);}return 0;
}
int ppa_do_scale_rotate_mirror(void*client,const ppa_srm_oper_config_t*c){
 auto*d=static_cast<MipiDsi*>(client);assert(!d->owner.available&&c->mode==PPA_TRANS_MODE_BLOCKING);
 assert(c->out.buffer==d->frame_buffer_&&c->out.pic_w==d->width_&&c->out.pic_h==d->height_);
 assert(c->out.buffer_size==d->frame_buffer_size_&&c->user_data==nullptr);++d->ppa_calls;
 if(d->reject_ppa)return 1; // rejection precedes any accepted DMA, as in IDF
 if(d->ppa_hook)d->ppa_hook();
 const unsigned sx=unsigned(c->scale_x*16),sy=unsigned(c->scale_y*16);
 const unsigned scaled_w=c->in.block_w*sx/16,scaled_h=c->in.block_h*sy/16;
 const bool swap=c->rotation_angle==PPA_SRM_ROTATION_ANGLE_90||c->rotation_angle==PPA_SRM_ROTATION_ANGLE_270;
 const unsigned w=swap?scaled_h:scaled_w,h=swap?scaled_w:scaled_h;
 auto*src=static_cast<const uint16_t*>(c->in.buffer);auto*out=static_cast<uint16_t*>(c->out.buffer);
 for(unsigned y=0;y<h;y++)for(unsigned x=0;x<w;x++){
  unsigned ix=x,iy=y;
  switch(c->rotation_angle){case PPA_SRM_ROTATION_ANGLE_90:ix=scaled_w-1-y;iy=x;break;case PPA_SRM_ROTATION_ANGLE_180:ix=scaled_w-1-x;iy=scaled_h-1-y;break;case PPA_SRM_ROTATION_ANGLE_270:ix=y;iy=scaled_h-1-x;break;default:break;}
  ix=ix*16/sx+c->in.block_offset_x;iy=iy*16/sy+c->in.block_offset_y;
  assert(ix<c->in.pic_w&&iy<c->in.pic_h);
  out[(y+c->out.block_offset_y)*c->out.pic_w+x+c->out.block_offset_x]=src[iy*c->in.pic_w+ix];
 }
 return 0;
}
''' + methods + r'''
using Matrix=std::vector<std::vector<uint16_t>>;
Matrix clockwise(Matrix a,unsigned turns){while(turns--){Matrix b(a[0].size(),std::vector<uint16_t>(a.size()));for(size_t y=0;y<a.size();y++)for(size_t x=0;x<a[0].size();x++)b[x][a.size()-1-y]=a[y][x];a=b;}return a;}
void complete(MipiDsi&d){++d.driver.callbacks;xSemaphoreGive(&d.completion);}
ppa_srm_oper_config_t operation(const std::vector<uint16_t>&input,unsigned stride,unsigned rows,unsigned width,unsigned height){ppa_srm_oper_config_t c{};c.in={input.data(),stride,rows,width,height,1,1,PPA_SRM_COLOR_MODE_RGB565};c.scale_x=c.scale_y=1;return c;}
int main(){
 // Independent dense transform + blit oracle, including nonzero input stride and output offsets.
 for(auto angle:{PPA_SRM_ROTATION_ANGLE_0,PPA_SRM_ROTATION_ANGLE_90,PPA_SRM_ROTATION_ANGLE_180,PPA_SRM_ROTATION_ANGLE_270})for(unsigned units:{8u,16u,24u}){
  MipiDsi d;d.shadow=d.pixels;d.buffer_=reinterpret_cast<uint8_t*>(d.shadow.data());
  std::vector<uint16_t>input(9*6);for(unsigned i=0;i<input.size();i++)input[i]=i+1;const auto immutable=input;
  auto c=operation(input,9,6,6,4);c.scale_x=c.scale_y=float(units)/16;c.rotation_angle=angle;
  Matrix dense(4*units/16,std::vector<uint16_t>(6*units/16));
  for(unsigned y=0;y<dense.size();y++)for(unsigned x=0;x<dense[0].size();x++)dense[y][x]=input[(y*16/units+1)*9+x*16/units+1];
  dense=clockwise(dense,(4-unsigned(angle))%4);auto expected=d.pixels;
  for(unsigned y=0;y<dense.size();y++)for(unsigned x=0;x<dense[0].size();x++)expected[(y+3)*d.width_+x+2]=dense[y][x];
  assert(d.draw_rgb565_transformed(c,input.size()*2,2,3,dense[0].size(),dense.size()));
  assert(d.pixels==expected&&d.shadow==expected&&input==immutable&&d.driver.submits==1&&d.driver.callbacks==1&&d.owner.available);
 }
 // Full and partial LVGL rectangles clip in logical space before one native transform.
 for(int rotation:{0,90,180,270})for(int mode:{0,1,2}){
  MipiDsi d;d.rotation_=rotation;int x=mode==1?-2:mode==2?d.get_width()-2:0,y=mode==1?-1:mode==2?d.get_height()-2:0;
  int w=mode?6:d.get_width(),h=mode?5:d.get_height(),stride=w+3,rows=h+2;
  std::vector<uint16_t>input(stride*rows);for(unsigned i=0;i<input.size();i++)input[i]=100+i;
  auto expected=d.pixels;
  for(int iy=0;iy<h;iy++)for(int ix=0;ix<w;ix++){
   int lx=x+ix,ly=y+iy;if(lx<0||ly<0||lx>=d.get_width()||ly>=d.get_height())continue;
   // Independent native point mapping from the existing public display rotation contract.
   int nx=lx,ny=ly;
   if(rotation==90){nx=d.width_-ly-1;ny=lx;}else if(rotation==180){nx=d.width_-lx-1;ny=d.height_-ly-1;}else if(rotation==270){nx=ly;ny=d.height_-lx-1;}
   expected[ny*d.width_+nx]=input[(iy+1)*stride+ix+1];
  }
  assert(d.draw_rotated_rgb565_(x,y,w,h,reinterpret_cast<const uint8_t*>(input.data()),1,1,2));assert(d.pixels==expected);
  // Touch coordinates are the inverse of display rotation, unchanged by backend selection.
  for(int ny:{0,8,16})for(int nx:{0,9,18}){int lx=nx,ly=ny;if(rotation==90){lx=ny;ly=d.width_-nx-1;}else if(rotation==180){lx=d.width_-nx-1;ly=d.height_-ny-1;}else if(rotation==270){lx=d.height_-ny-1;ly=nx;}assert(lx>=0&&ly>=0&&lx<d.get_width()&&ly<d.get_height());d.rotate_(lx,ly);assert(lx==nx&&ly==ny);}
 }

 // Without a runtime PPA client, logical LVGL data still rotates through the
 // unchanged scalar Display fallback, despite omitted upstream rotate_buf.
 for(int rotation:{0,90,180,270}){
  MipiDsi d;d.rotation_=rotation;d.transform_client_=nullptr;
  std::vector<uint16_t>input(6*4);for(unsigned i=0;i<input.size();i++)input[i]=0xf800+i;
  auto expected=d.pixels;
  for(int y=0;y<4;y++)for(int x=0;x<6;x++){int nx=x+2,ny=y+3;d.rotate_(nx,ny);expected[ny*d.width_+nx]=input[y*6+x];}
  d.draw_pixels_at(2,3,6,4,reinterpret_cast<const uint8_t*>(input.data()),display::COLOR_ORDER_RGB,display::COLOR_BITNESS_565,false,0,0,0);
  assert(d.pixels==expected&&d.ppa_calls==0);
  // Already-oriented H.264 / old JPEG scratch input bypasses logical rotation.
  MipiDsi native;native.rotation_=rotation;native.transform_client_=nullptr;auto before=native.pixels;
  assert(native.draw_native_rgb565(2,3,6,4,reinterpret_cast<const uint8_t*>(input.data()),input.size()*2));
  for(int y=0;y<4;y++)for(int x=0;x<6;x++)before[(y+3)*native.width_+x+2]=input[y*6+x];
  assert(native.pixels==before&&native.ppa_calls==0&&native.driver.callbacks==1);
 }
 // Input BGR and explicit painter clipping still delegate the existing pixel path.
 for(bool clipped:{false,true}){
  MipiDsi d;d.rotation_=270;d.clipping=clipped;std::vector<uint16_t>input(6*4,0x001f);auto expected=d.pixels;
  for(int y=0;y<4;y++)for(int x=0;x<6;x++){int lx=x+1,ly=y+1;if(clipped&&!d.clipping_rectangle_.back().inside(lx,ly))continue;int nx=lx,ny=ly;d.rotate_(nx,ny);expected[ny*d.width_+nx]=clipped?0x001f:0xf800;}
  d.draw_pixels_at(1,1,6,4,reinterpret_cast<const uint8_t*>(input.data()),clipped?display::COLOR_ORDER_RGB:display::COLOR_ORDER_BGR,display::COLOR_BITNESS_565,false,0,0,0);
  assert(d.pixels==expected&&d.ppa_calls==0&&d.legacy_calls==1);
 }
 std::vector<uint16_t>input(9*6,99);auto c=operation(input,9,6,6,4);MipiDsi invalid;auto original=invalid.pixels;
 assert(!invalid.draw_rgb565_transformed(c,1,2,3,6,4));assert(!invalid.draw_rgb565_transformed(c,input.size()*2,-1,3,6,4));
 assert(!invalid.draw_rgb565_transformed(c,input.size()*2,17,3,6,4));assert(!invalid.draw_rgb565_transformed(c,input.size()*2,2,3,7,4));
 auto bad=c;bad.scale_x=std::nanf("");assert(!invalid.draw_rgb565_transformed(bad,input.size()*2,2,3,6,4));
 bad=c;bad.in.block_offset_x=8;assert(!invalid.draw_rgb565_transformed(bad,input.size()*2,2,3,6,4));
 bad=c;bad.in.buffer=invalid.frame_buffer_;assert(!invalid.draw_rgb565_transformed(bad,invalid.frame_buffer_size_,2,3,6,4));
 invalid.color_depth_=24;assert(!invalid.draw_rgb565_transformed(c,input.size()*2,2,3,6,4));invalid.color_depth_=1;invalid.big_endian_=true;assert(!invalid.supports_rgb565_transform());invalid.big_endian_=false;invalid.transform_client_=nullptr;assert(!invalid.draw_rgb565_transformed(c,input.size()*2,2,3,6,4));
 assert(invalid.ppa_calls==0&&invalid.driver.submits==0&&invalid.pixels==original);
 MipiDsi ppa_error;ppa_error.reject_ppa=true;assert(!ppa_error.draw_rgb565_transformed(c,input.size()*2,2,3,6,4));assert(ppa_error.driver.callbacks==0&&ppa_error.driver.submits==0&&ppa_error.owner.available&&ppa_error.pixels==original);
 MipiDsi lcd_error;lcd_error.driver.reject=true;assert(!lcd_error.draw_rgb565_transformed(c,input.size()*2,2,3,6,4));assert(lcd_error.driver.callbacks==0&&lcd_error.owner.available&&lcd_error.failed);

 MipiDsi sanitize;auto hostile=c;hostile.out.buffer=const_cast<uint16_t*>(input.data());hostile.out.pic_w=1;hostile.out.buffer_size=1;hostile.out.block_offset_x=UINT32_MAX;hostile.mode=0;hostile.user_data=&input;
 assert(sanitize.draw_rgb565_transformed(hostile,input.size()*2,2,3,6,4)&&sanitize.driver.callbacks==1);
 // Admission is rechecked after the framebuffer owner becomes available.
 MipiDsi stale;std::atomic<bool>current{true};auto admit=[](void*p){return static_cast<std::atomic<bool>*>(p)->load();};
 xSemaphoreTake(&stale.owner,portMAX_DELAY);
 auto queued=std::async(std::launch::async,[&]{return stale.draw_rgb565_transformed(c,input.size()*2,2,3,6,4,admit,&current);});
 assert(queued.wait_for(20ms)==std::future_status::timeout);current=false;stale.pixels.assign(stale.pixels.size(),0x5678);xSemaphoreGive(&stale.owner);
 assert(!queued.get()&&stale.ppa_calls==0&&stale.driver.submits==0&&stale.owner.available);
 // Blocking PPA retains source and framebuffer ownership. A separate screenshot
 // copy cannot enter until both PPA and the one LCD callback have finished.
 MipiDsi concurrent;concurrent.driver.defer=true;std::promise<void>ppa_entered,release_ppa;auto permit=release_ppa.get_future().share();
 concurrent.ppa_hook=[&]{if(concurrent.ppa_calls==1){ppa_entered.set_value();permit.wait();}};
 auto first=std::async(std::launch::async,[&]{return concurrent.draw_rgb565_transformed(c,input.size()*2,2,3,6,4);});
 ppa_entered.get_future().wait();assert(first.wait_for(20ms)==std::future_status::timeout&&concurrent.driver.submits==0);
 std::vector<uint16_t>snapshot(concurrent.pixels.size(),0xbbbb);
 assert(!concurrent.copy_frame_buffer_rgb565(reinterpret_cast<uint8_t*>(snapshot.data()),snapshot.size()*2));
 auto second=std::async(std::launch::async,[&]{return concurrent.draw_rgb565_transformed(c,input.size()*2,2,3,6,4);});
 assert(second.wait_for(20ms)==std::future_status::timeout&&concurrent.ppa_calls==1);release_ppa.set_value();
 xSemaphoreTake(&concurrent.driver.entered,portMAX_DELAY);assert(first.wait_for(20ms)==std::future_status::timeout);
 assert(!concurrent.copy_frame_buffer_rgb565(reinterpret_cast<uint8_t*>(snapshot.data()),snapshot.size()*2));
 complete(concurrent);assert(first.get());xSemaphoreTake(&concurrent.driver.entered,portMAX_DELAY);assert(second.wait_for(20ms)==std::future_status::timeout);complete(concurrent);assert(second.get());
 assert(concurrent.driver.callbacks==2&&concurrent.driver.submits==2&&concurrent.owner.available);
 assert(concurrent.copy_frame_buffer_rgb565(reinterpret_cast<uint8_t*>(snapshot.data()),snapshot.size()*2)&&snapshot==concurrent.pixels);
}
'''
    cpp = tmp_path / "owned.cpp"
    cpp.write_text(code)
    binary = tmp_path / "owned"
    result = subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pthread",
                             "-fsanitize=address,undefined", "-fno-pie", "-no-pie", str(cpp), "-o", str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr


def test_owned_rotation_codegen_preserves_lvgl_capacity_and_provider_fallbacks(tmp_path):
    import sys
    for depth, order, owned in [(16, "little_endian", True), (24, "little_endian", False), (16, "big_endian", False)]:
        folder = tmp_path / f"depth-{depth}-{order}"
        folder.mkdir()
        config = folder / "screen.yaml"
        config.write_text(f'''esphome:
  name: owned-framebuffer
  build_path: {folder / 'build'}
esp32:
  board: esp32-p4-evboard
  framework:
    type: esp-idf
network:
psram:
esp_ldo:
  - channel: 3
    voltage: 2.5V
external_components:
  - source:
      type: local
      path: {DRIVER.parent}
    components: [mipi_dsi]
display:
  - platform: mipi_dsi
    id: screen
    model: WAVESHARE-10.1-DSI-TOUCH-A
    color_depth: {depth}
    byte_order: {order}
lvgl:
  displays: [screen]
  rotation: 270
  byte_order: {order}
''')
        result = subprocess.run([sys.executable, "-m", "esphome", "compile", "--only-generate", str(config)],
                                capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        source = folder / "build/src"
        generated = (source / "main.cpp").read_text()
        defines = (source / "esphome/core/defines.h").read_text()
        assert ("#define USE_MIPI_DSI_RGB565_OWNED_TRANSFORM" in defines) == owned
        assert "{screen}, 0.0f, false" in generated
        assert f"lvgl::ROTATION_{'HARDWARE' if owned else 'SOFTWARE'}" in generated
        assert "DISPLAY_ROTATION_270_DEGREES" in generated


def test_consumer_provider_handshake_uses_the_merged_package_graph(tmp_path):
    import shutil
    import sys
    # The consumer boundary needs only its stable build-time capability;
    # actual old-provider C++ interoperability is covered in Native's tests.
    for new_provider, new_consumer, packaged in [(False, False, False), (False, True, False),
                                                (True, False, False), (True, True, False),
                                                (True, False, True), (True, True, True)]:
        folder = tmp_path / f"p{int(new_provider)}-c{int(new_consumer)}-package{int(packaged)}"
        folder.mkdir()
        components = folder / "components"
        consumer = components / "p4_video_renderer"
        consumer.mkdir(parents=True)
        consumer.joinpath("__init__.py").write_text(
            "import esphome.config_validation as cv\nCONFIG_SCHEMA=cv.Schema({})\n"
            + ("MIPI_NATIVE_COORDINATE_BLITS=True\n" if new_consumer else "")
            + "async def to_code(config):\n    pass\n"
        )
        provider = DRIVER.parent
        if not new_provider:
            legacy = components / "mipi_dsi"
            shutil.copytree(DRIVER, legacy, ignore=shutil.ignore_patterns("__pycache__"))
            for name in ("display.py", "mipi_dsi.h", "mipi_dsi.cpp"):
                result = subprocess.run(["git", "show", f"a7ba2d27:esphome/components/mipi_dsi/{name}"],
                                        cwd=ROOT, check=True, capture_output=True, text=True)
                legacy.joinpath(name).write_text(result.stdout)
            provider = components
        declaration = "p4_video_renderer: {}\n"
        if packaged:
            folder.joinpath("consumer.yaml").write_text(declaration)
            declaration = "packages:\n  native_consumer: !include consumer.yaml\n"
        config = folder / "screen.yaml"
        # Consumer declaration is before the display here, while the basic
        # provider test leaves it absent. Package expansion must behave equally.
        config.write_text(f'''esphome:
  name: provider-consumer-contract
  build_path: {folder / 'build'}
esp32:
  board: esp32-p4-evboard
  framework:
    type: esp-idf
network:
psram:
esp_ldo:
  - channel: 3
    voltage: 2.5V
external_components:
  - source:
      type: local
      path: {provider}
    components: [mipi_dsi]
  - source:
      type: local
      path: {components}
    components: [p4_video_renderer]
{declaration}display:
  - platform: mipi_dsi
    id: screen
    model: WAVESHARE-10.1-DSI-TOUCH-A
lvgl:
  displays: [screen]
  rotation: 270
''')
        result = subprocess.run([sys.executable, "-m", "esphome", "compile", "--only-generate", str(config)],
                                capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        generated = folder / "build/src"
        main = generated.joinpath("main.cpp").read_text()
        defines = generated.joinpath("esphome/core/defines.h").read_text()
        owned = new_provider and new_consumer
        assert ("#define USE_MIPI_DSI_RGB565_OWNED_TRANSFORM" in defines) == owned
        assert f"lvgl::ROTATION_{'HARDWARE' if owned else 'SOFTWARE'}" in main
        assert "{screen}, 0.0f, false" in main
