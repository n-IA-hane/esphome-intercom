"""Execute the production screenshot ownership and pixel assembly contracts."""

from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEADERS = ROOT / "esphome/components/esp_screenshot"


@pytest.mark.parametrize("sanitize", [False, True])
def test_screenshot_pixel_coverage_and_immutable_http_leases(tmp_path, sanitize):
    source = tmp_path / "screenshot.cpp"
    source.write_text(r"""
#include "snapshot_store.h"
#include <array>
#include <cassert>
#include <cstring>
using namespace esphome::esp_screenshot;
int main() {
  SnapshotSlots slots;
  assert(slots.acquire() == -1);
  int first = slots.begin_write();
  assert(first == 0);
  assert(slots.begin_write() == -1);
  assert(!slots.publish(1));
  assert(slots.publish(first));
  const int reader = slots.acquire();
  assert(reader == first);
  int next = slots.begin_write();
  assert(next == 1);
  assert(slots.publish(next));
  // A slow HTTP reader keeps the old image alive after a new publication.
  assert(slots.begin_write() == -1);
  slots.release(reader);
  assert(slots.begin_write() == first);
  slots.cancel_write();
  // A failed capture leaves the preceding successful image published.
  const int latest = slots.acquire();
  assert(latest == next);
  slots.release(latest);
  std::array<uint8_t, 24> result{};
  std::array<uint8_t, 2> coverage{};
  FrameAssembler frame;
  frame.reset(result.data(), coverage.data(), 4, 3);
  const uint8_t top[12] = {1,2,3,4,5,6,7,8,99,99,99,99};
  assert(frame.add(0, 0, 4, 1, top, sizeof(top)));
  assert(!frame.complete());
  // Repeated/overlapping rectangles must not inflate coverage.
  assert(frame.add(0, 0, 4, 1, top, sizeof(top)));
  assert(!frame.complete());
  assert(!frame.add(-1, 1, 4, 1, top, sizeof(top)));
  assert(!frame.add(1, 1, 4, 1, top, sizeof(top)));
  assert(!frame.add(0, 1, 4, 1, top, 7));
  assert(!frame.add(0, 1, 4, 1, nullptr, 8));
  const uint8_t lower[24] = {11,12,13,14,15,16,17,18,88,88,88,88,
                            21,22,23,24,25,26,27,28,77,77,77,77};
  assert(frame.add(0, 1, 4, 2, lower, 12));
  assert(frame.complete());
  assert(std::memcmp(result.data(), top, 8) == 0);
  assert(std::memcmp(result.data()+8, lower, 8) == 0);
  assert(std::memcmp(result.data()+16, lower+12, 8) == 0);
  frame.reset(result.data(), coverage.data(), 4, 3);
  assert(!frame.complete());
}
""")
    binary = tmp_path / "screenshot"
    command = [
        "g++",
        "-std=c++17",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-I",
        str(HEADERS),
        str(source),
        "-o",
        str(binary),
    ]
    if sanitize:
        command[1:1] = [
            "-fsanitize=address,undefined",
            "-fno-omit-frame-pointer",
            "-fno-pie",
            "-no-pie",
        ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)


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
constexpr int COLOR_BITNESS_565=16;
}
struct MipiDsi {
 int color_depth_=16; bool big_endian_=false; size_t width_=3,height_=2;
 uint8_t *frame_buffer_=nullptr;size_t frame_buffer_size_=12;
 void *submit_mutex_=this;int rotation_=0;
 bool is_failed(){return false;}
 int get_width(){return rotation_==90||rotation_==270?2:3;}
 int get_height(){return rotation_==90||rotation_==270?3:2;}
 bool copy_frame_buffer_rgb565(uint8_t*,size_t);
"""
        + rotate
        + "\n};\n"
        + method
        + r"""
int main(){
 MipiDsi d;std::array<uint16_t,6> input{1,2,3,4,5,6},output{};
 d.frame_buffer_=reinterpret_cast<uint8_t*>(input.data());
 assert(d.copy_frame_buffer_rgb565(reinterpret_cast<uint8_t*>(output.data()),12));
 assert(output==input&&!locked&&syncs==1);
 d.rotation_=90;
 assert(d.copy_frame_buffer_rgb565(reinterpret_cast<uint8_t*>(output.data()),12));
 assert((output==std::array<uint16_t,6>{3,6,2,5,1,4}));
 d.rotation_=180;
 assert(d.copy_frame_buffer_rgb565(reinterpret_cast<uint8_t*>(output.data()),12));
 assert((output==std::array<uint16_t,6>{6,5,4,3,2,1}));
 d.rotation_=270;
 assert(d.copy_frame_buffer_rgb565(reinterpret_cast<uint8_t*>(output.data()),12));
 assert((output==std::array<uint16_t,6>{4,1,5,2,6,3}));
 d.rotation_=0;d.big_endian_=true;
 for(auto &pixel:input)pixel=convert_big_endian(pixel);
 assert(d.copy_frame_buffer_rgb565(reinterpret_cast<uint8_t*>(output.data()),12));
 assert((output==std::array<uint16_t,6>{1,2,3,4,5,6}));
 const auto before=output;const int previous_syncs=syncs;
 locked=true;
 assert(!d.copy_frame_buffer_rgb565(reinterpret_cast<uint8_t*>(output.data()),12));
 assert(output==before&&syncs==previous_syncs);
 locked=false;sync_failure=true;
 assert(!d.copy_frame_buffer_rgb565(reinterpret_cast<uint8_t*>(output.data()),12));
 assert(output==before&&!locked);
 sync_failure=false;d.color_depth_=24;
 assert(!d.copy_frame_buffer_rgb565(reinterpret_cast<uint8_t*>(output.data()),12));
 assert(!locked);
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


def test_http_errors_survive_the_native_wrapper_status_mapping(tmp_path):
    import esphome

    native = (
        Path(esphome.__file__).parent / "components/web_server_idf/web_server_idf.cpp"
    )
    wrapper = (
        native.read_text()
        .split("void AsyncWebServerRequest::init_response_(", 1)[1]
        .split("\n#ifdef USE_WEBSERVER_AUTH", 1)[0]
    )
    wrapper = "void AsyncWebServerRequest::init_response_(" + wrapper
    source = (HEADERS / "esp_screenshot.cpp").read_text()
    helper = source.split("static void send_http_status(", 1)[1].split(
        "\nvoid EspScreenshot::setup()", 1
    )[0]
    helper = "static void send_http_status(" + helper
    harness = tmp_path / "http.cpp"
    harness.write_text(
        r"""
#include <cassert>
#include <string>
#include <vector>
#define HTTPD_200 "200 OK"
#define HTTPD_204 "204 No Content"
#define HTTPD_400 "400 Bad Request"
#define HTTPD_401 "401 Unauthorized"
#define HTTPD_404 "404 Not Found"
#define HTTPD_409 "409 Conflict"
#define HTTPD_422 "422 Unprocessable Entity"
#define HTTPD_500 "500 Internal Server Error"
struct httpd_req_t {std::string status,type,body;};
void httpd_resp_set_status(httpd_req_t *r,const char *status){r->status=status;}
void httpd_resp_set_type(httpd_req_t *r,const char *type){r->type=type;}
void httpd_resp_set_hdr(httpd_req_t*,const char*,const char*){}
struct Header{const char *name,*value;};
struct DefaultHeaders {
 std::vector<Header> headers_;
 static DefaultHeaders &Instance(){static DefaultHeaders h;return h;}
};
struct AsyncWebServerResponse {std::string content;};
struct AsyncWebServerRequest {
 httpd_req_t raw;AsyncWebServerResponse *rsp_{};
 ~AsyncWebServerRequest(){delete rsp_;}
 operator httpd_req_t*(){return &raw;}
 void init_response_(AsyncWebServerResponse*,int,const char*);
 AsyncWebServerResponse *beginResponse(int code,const char *type,const std::string &body){
  auto *res=new AsyncWebServerResponse{body};init_response_(res,code,type);return res;
 }
 void send(AsyncWebServerResponse *res){raw.body=res->content;}
};
"""
        + wrapper
        + helper
        + r"""
int main(){
 AsyncWebServerRequest witness;
 witness.beginResponse(410,"text/plain","expired");
 assert(witness.raw.status==HTTPD_500); // Exact upstream implementation.
 AsyncWebServerRequest expired;
 send_http_status(&expired,"410 Gone","Screenshot identifier has expired");
 assert(expired.raw.status=="410 Gone"&&expired.raw.type=="text/plain");
 assert(expired.raw.body=="Screenshot identifier has expired");
 AsyncWebServerRequest first;
 send_http_status(&first,"503 Service Unavailable","Take a screenshot first");
 assert(first.raw.status=="503 Service Unavailable"&&first.raw.body=="Take a screenshot first");
}
"""
    )
    binary = tmp_path / "http"
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


def test_setup_honors_explicit_memory_and_worker_parameters_without_fallback(tmp_path):
    source = (HEADERS / "esp_screenshot.cpp").read_text()
    methods = (
        "void EspScreenshot::setup()"
        + source.split("void EspScreenshot::setup()", 1)[1].split(
            "\nbool EspScreenshot::capture()", 1
        )[0]
    )
    harness = tmp_path / "allocation.cpp"
    harness.write_text(
        r"""
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <string>
#include <unordered_map>
#define USE_ESP_SCREENSHOT_MIPI_DSI
constexpr int MALLOC_CAP_8BIT=1,MALLOC_CAP_INTERNAL=2,MALLOC_CAP_SPIRAM=4;
constexpr int JPEG_PIXEL_FORMAT_RGB565_LE=1,JPEG_SUBSAMPLE_444=1,JPEG_ERR_OK=0;
constexpr int tskNO_AFFINITY=0x7fffffff;
constexpr unsigned portNUM_PROCESSORS=2;
static const char *TAG="test";
template<typename... Args>void sink(Args...args){((void)args,...);}
#define ESP_LOGE(...) sink(__VA_ARGS__)
static std::unordered_map<void*,int> allocations;
static int allocation_count=0,fail_allocation=0,task_creations=0;
static bool encoder_error=false;
void *heap_caps_malloc(size_t n,int caps){
 if(++allocation_count==fail_allocation)return nullptr;
 auto *p=std::malloc(n);allocations[p]=caps;return p;
}
void *heap_caps_aligned_alloc(size_t,size_t n,int caps){return heap_caps_malloc(n,caps);}
void heap_caps_free(void *p){if(p){assert(allocations.erase(p)==1);std::free(p);}}
using StackType_t=uint8_t;
using TaskHandle_t=void*;
struct StaticTask_t {int internal;};
struct StaticSemaphore_t {int internal;};
void *xSemaphoreCreateMutexStatic(StaticSemaphore_t *p){return p;}
struct jpeg_enc_config_t {int width{},height{},src_type{},subsampling{},quality{};bool task_enable{};};
#define DEFAULT_JPEG_ENC_CONFIG() jpeg_enc_config_t{}
using jpeg_enc_handle_t=void*;
int jpeg_enc_open(jpeg_enc_config_t*,void **h){if(encoder_error)return 1;*h=reinterpret_cast<void*>(1);return 0;}
void jpeg_enc_close(void*){}
uint32_t esp_random(){return 42;}
static int expected_core,expected_priority,expected_stack_caps;
static size_t expected_stack_bytes;
TaskHandle_t xTaskCreateStaticPinnedToCore(void(*)(void*),const char*,size_t n,void*,unsigned priority,
                                          StackType_t *stack,StaticTask_t *tcb,int core){
 assert(allocations.at(stack)==expected_stack_caps&&n==expected_stack_bytes);
 assert(int(priority)==expected_priority&&core==expected_core&&tcb!=nullptr);
 task_creations++;return reinterpret_cast<void*>(2);
}
struct Display{int get_width(){return 8;}int get_height(){return 8;}};
struct Server{void add_handler(void*){}void init(){}};
struct TextSensor {
 std::string state;
 void publish_state(const std::string &value){state=value;}
};
struct EspScreenshot {
 Display *mipi_display_{};Server *web_server_{};unsigned width_{},height_{};
 size_t pixel_size_{},jpeg_capacity_=4096;uint8_t *pixels_{},*coverage_{},*jpeg_[2]{};
 StackType_t *worker_stack_{};StaticTask_t worker_tcb_{};StaticSemaphore_t mutex_storage_{};void *mutex_{};
 jpeg_enc_handle_t encoder_{};uint8_t quality_=85;uint32_t boot_id_{};TaskHandle_t worker_handle_{};
 uint32_t task_stack_size_=6144;int8_t task_core_=-1;uint8_t task_priority_=1;
 bool task_stack_in_psram_=true,buffer_in_psram_=true,failed=false,loop_disabled=false;
 TextSensor *capture_status_sensor_{};
 void setup();void fail_setup_(const char*);void publish_status_(const std::string&);
 void mark_failed(){failed=true;}
 void disable_loop(){loop_disabled=true;}static void worker_entry_(void*){}
};
"""
        + methods
        + r"""
int main(){
 Display display;Server server;
 for(bool external:{false,true}){
  EspScreenshot component;component.mipi_display_=&display;component.web_server_=&server;
  component.task_core_=1;component.task_priority_=3;component.task_stack_size_=8192;
  component.task_stack_in_psram_=external;component.buffer_in_psram_=external;
  expected_core=1;expected_priority=3;expected_stack_bytes=8192;
  expected_stack_caps=MALLOC_CAP_8BIT|(external?MALLOC_CAP_SPIRAM:MALLOC_CAP_INTERNAL);
  component.setup();assert(!component.failed&&component.loop_disabled);
  assert(allocations.size()==4);
  for(const auto &entry:allocations)assert(entry.second==expected_stack_caps);
  component.fail_setup_("test fixture has no real worker");assert(allocations.empty());
 }
 TextSensor status;
 EspScreenshot default_core;default_core.mipi_display_=&display;default_core.web_server_=&server;
 default_core.capture_status_sensor_=&status;
 expected_core=tskNO_AFFINITY;expected_priority=1;expected_stack_bytes=6144;
 expected_stack_caps=MALLOC_CAP_8BIT|MALLOC_CAP_SPIRAM;
 default_core.setup();assert(!default_core.failed&&status.state=="No screenshot");
 default_core.fail_setup_("test fixture has no real worker");assert(allocations.empty());
 const int before=task_creations;
 EspScreenshot invalid_core;invalid_core.task_core_=2;invalid_core.capture_status_sensor_=&status;
 invalid_core.setup();assert(invalid_core.failed&&allocations.empty()&&task_creations==before);
 assert(status.state=="Error: Requested worker core is unavailable");
 for(int failure=1;failure<=4;failure++){
  EspScreenshot component;component.mipi_display_=&display;component.web_server_=&server;
  component.capture_status_sensor_=&status;
  allocation_count=0;fail_allocation=failure;
  component.setup();assert(component.failed&&component.worker_handle_==nullptr);
  assert(allocations.empty()&&task_creations==before);
  assert(status.state=="Error: Unable to preallocate screenshot resources; no memory fallback");
 }
 fail_allocation=0;encoder_error=true;
 EspScreenshot failed;failed.mipi_display_=&display;failed.web_server_=&server;
 failed.capture_status_sensor_=&status;
 failed.setup();assert(failed.failed&&allocations.empty()&&task_creations==before);
 assert(status.state=="Error: Unable to initialize the software JPEG encoder");
}
"""
    )
    binary = tmp_path / "allocation"
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


def test_capture_status_reports_completion_and_failures_without_replacing_last_image(
    tmp_path,
):
    source = (HEADERS / "esp_screenshot.cpp").read_text()
    methods = "void EspScreenshot::publish_status_(" + source.split(
        "void EspScreenshot::publish_status_(", 1
    )[1].split("\nbool EspScreenshot::copy_display_()", 1)[0]
    harness = tmp_path / "lifecycle.cpp"
    harness.write_text(
        r"""
#include "snapshot_store.h"
#include <atomic>
#include <cassert>
#include <cstdio>
#include <string>
#include <vector>
using namespace esphome::esp_screenshot;
static const char *TAG="test";
template<typename... Args>void sink(Args...args){((void)args,...);}
#define ESP_LOGW(...) sink(__VA_ARGS__)
#define ESP_LOGD(...) sink(__VA_ARGS__)
constexpr int portMAX_DELAY=1;
static bool locked=false;
static int notifications=0;
void xSemaphoreTake(void*,int){assert(!locked);locked=true;}
void xSemaphoreGive(void*){assert(locked);locked=false;}
void xTaskNotifyGive(void*){notifications++;}
struct TextSensor {
 std::string state;std::vector<std::string> history;
 void publish_state(const std::string &value){state=value;history.push_back(value);}
};
struct EspScreenshot {
 TextSensor *capture_id_sensor_{},*capture_status_sensor_{};
 std::atomic_bool busy_{false},requested_{false},completed_{false},stopping_{false};
 SnapshotSlots slots_{};int writing_slot_=-1;void *worker_handle_=this,*mutex_=this;
 bool failed=false,copy_available=true,encode_success_=false,loop_enabled=false;
 unsigned width_=8,height_=8;size_t jpeg_size_[2]{};char capture_id_[2][18]{};
 bool capture();void cancel_capture_(const char*);void loop();
 void publish_status_(const std::string&);
 bool is_failed(){return failed;}
 bool copy_display_(){return copy_available;}
 void enable_loop_soon_any_context(){loop_enabled=true;}
 void disable_loop(){loop_enabled=false;}
 int published_slot(){int index=slots_.acquire();if(index>=0)slots_.release(index);return index;}
 void finish_encoder(bool success,const char *id=""){
  assert(writing_slot_>=0);
  encode_success_=success;
  if(success){
   std::snprintf(capture_id_[writing_slot_],18,"%s",id);
   jpeg_size_[writing_slot_]=100;
   assert(slots_.publish(writing_slot_));
  }
  completed_.store(true);
  loop();
 }
};
"""
        + methods
        + r"""
int main(){
 TextSensor status,id;EspScreenshot component;
 component.capture_status_sensor_=&status;component.capture_id_sensor_=&id;
 assert(component.capture()&&component.loop_enabled&&status.state=="Capturing");
 assert(id.history.empty()&&component.published_slot()==-1);
 assert(!component.capture()&&status.state=="Busy");
 component.loop();assert(notifications==1&&id.history.empty());
 component.finish_encoder(true,"boot0001-00000001");
 assert(status.state=="Ready"&&id.state=="boot0001-00000001"&&!component.busy_.load());
 const int first=component.published_slot();
 assert(first>=0&&!component.loop_enabled);

 // Encoding failure and unavailable display keep the successful image and ID.
 assert(component.capture());component.loop();
 component.finish_encoder(false);
 assert(status.state=="Error: JPEG encoding failed or exceeded max_jpeg_size");
 assert(component.published_slot()==first&&id.history.size()==1&&!component.busy_.load());
 component.copy_available=false;
 assert(component.capture());component.loop();
 assert(status.state=="Error: A complete display capture is unavailable");
 assert(component.published_slot()==first&&id.history.size()==1&&!component.busy_.load());

 // A reader leasing the old image prevents a third capture from reusing it.
 component.copy_available=true;
 const int reader=component.slots_.acquire();assert(reader==first);
 assert(component.capture());component.loop();
 component.finish_encoder(true,"boot0001-00000002");
 const int second=component.published_slot();assert(second!=first);
 assert(component.capture());component.loop();
 assert(status.state=="Error: Image readers still own the available buffer");
 assert(component.published_slot()==second&&id.state=="boot0001-00000002");
 assert(id.history.size()==2&&!component.busy_.load());component.slots_.release(reader);

 component.failed=true;
 assert(!component.capture()&&status.state=="Error: Screenshot is unavailable");
 assert(component.published_slot()==second&&id.history.size()==2);
 component.failed=false;component.stopping_.store(true);
 assert(!component.capture()&&status.state=="Error: Screenshot is unavailable");

 // Existing configurations without either optional sensor still work.
 EspScreenshot optional;
 assert(optional.capture());optional.loop();optional.finish_encoder(true,"boot0002-00000001");
 assert(optional.published_slot()>=0&&!optional.busy_.load()&&!locked);
}
"""
    )
    binary = tmp_path / "lifecycle"
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
            "-I",
            str(HEADERS),
            str(harness),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)
