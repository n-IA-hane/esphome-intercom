"""Exercise the production display transfer owner with asynchronous completions."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_transfer_completion_is_owned_and_errors_do_not_wait(tmp_path):
    source = (ROOT / 'esphome/components/mipi_dsi/mipi_dsi.cpp').read_text()
    begin = source.index('bool MipiDsi::submit_bitmap_(')
    end = source.index('\nvoid MipiDsi::write_to_display_', begin)
    method = source[begin:end]
    cpp = tmp_path / 'transfer.cpp'
    cpp.write_text(r'''
#include <cassert>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <future>
#include <mutex>
#include <cstdint>
using namespace std::chrono_literals;
struct Semaphore {
 std::mutex mutex;std::condition_variable changed;bool available;
 explicit Semaphore(bool initial):available(initial){}
};
constexpr int portMAX_DELAY=-1,ESP_OK=0;
using esp_err_t=int;
bool xSemaphoreTake(Semaphore *s,int wait){
 std::unique_lock lock(s->mutex);
 if(!wait && !s->available)return false;
 s->changed.wait(lock,[&]{return s->available;});s->available=false;return true;
}
void xSemaphoreGive(Semaphore *s){
 {std::lock_guard lock(s->mutex);s->available=true;}s->changed.notify_one();
}
struct Driver {
 std::atomic<int> submitted{0};bool reject=false;Semaphore entered{false};
};
int esp_lcd_panel_draw_bitmap(Driver *d,int,int,int,int,const uint8_t*){
 ++d->submitted;xSemaphoreGive(&d->entered);return d->reject ? 1 : 0;
}
#define LOG_STR(x) x
struct MipiDsi {
 Driver driver;Driver *handle_=&driver;Semaphore completion{false},owner{true};
 Semaphore *io_lock_=&completion,*submit_mutex_=&owner;bool failed=false;
 bool is_failed(){return failed;}void smark_failed(const char*,int){failed=true;}
 bool submit_bitmap_(int,int,int,int,const uint8_t*);
 bool submit_bitmap_locked_(int,int,int,int,const uint8_t*);
};
''' + method + r'''
int main(){
 MipiDsi panel;
 // A stale IRQ cannot release the next transaction's caller-owned buffer.
 xSemaphoreGive(&panel.completion);
 uint8_t first[2]={1,2},second[2]={3,4};
 auto one=std::async(std::launch::async,[&]{return panel.submit_bitmap_(0,0,1,1,first);});
 xSemaphoreTake(&panel.driver.entered,portMAX_DELAY);
 assert(one.wait_for(20ms)==std::future_status::timeout);
 std::promise<void> entered;auto began=entered.get_future();
 auto two=std::async(std::launch::async,[&]{entered.set_value();return panel.submit_bitmap_(0,0,1,1,second);});
 began.wait();assert(two.wait_for(20ms)==std::future_status::timeout);
 assert(panel.driver.submitted==1);
 xSemaphoreGive(&panel.completion);assert(one.get());
 xSemaphoreTake(&panel.driver.entered,portMAX_DELAY);
 assert(panel.driver.submitted==2 && two.wait_for(20ms)==std::future_status::timeout);
 xSemaphoreGive(&panel.completion);assert(two.get());
 assert(xSemaphoreTake(&panel.owner,0));xSemaphoreGive(&panel.owner);
 MipiDsi broken;broken.driver.reject=true;
 auto error=std::async(std::launch::async,[&]{return broken.submit_bitmap_(0,0,1,1,first);});
 assert(error.wait_for(200ms)==std::future_status::ready);
 assert(!error.get() && broken.failed);
 assert(xSemaphoreTake(&broken.owner,0));xSemaphoreGive(&broken.owner);
 assert(!broken.submit_bitmap_(0,0,1,1,first) && broken.driver.submitted==1);
}
''')
    binary = tmp_path / 'transfer'
    subprocess.run(['g++', '-std=c++20', '-pthread', str(cpp), '-o', str(binary)], check=True, capture_output=True, text=True)
    subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=5)
