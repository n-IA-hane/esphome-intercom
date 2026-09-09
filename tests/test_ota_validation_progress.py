"""Run the real OTA finalizer with two independently bounded IDF validations."""

from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["full_audio_maintenance.yaml", "full_audio_lvgl_maintenance.yaml"])
def test_full_maintenance_composes_the_selected_ota_backend(name):
    from esphome import yaml_util
    from esphome.components.packages import resolve_packages

    maintenance = yaml_util.load_yaml(ROOT / "packages/ota" / name)
    config = resolve_packages({
        "substitutions": {"ext_components_source": "selected-local-components"},
        "packages": {"maintenance": maintenance},
    })
    assert config["substitutions"]["ext_components_source"] == "selected-local-components"
    providers = [entry for entry in config["external_components"] if "ota" in entry["components"]]
    assert len(providers) == 1
    assert providers[0]["source"] == "${ext_components_source}"


def test_ota_validation_progress_preserves_watchdog_and_failure_semantics(tmp_path):
    source = (ROOT / "esphome/components/ota/ota_backend_esp_idf.cpp").read_text()
    start = source.index("OTAResponseTypes IDFOTABackend::end()")
    method = source[start:source.index("\n}\n", start) + 2]
    harness = r'''
#include <cassert>
#include <cstdlib>
#include <cstdint>
#include <cstddef>
template<class... T> void log(T...) {}
#define ESP_LOGI(...) log(__VA_ARGS__)
#define ESP_LOGE(...) log(__VA_ARGS__)
constexpr const char *TAG="ota";
uint32_t clock_ms=0,last_feed=0,budget=5000;
uint32_t millis(){return clock_ms;}
struct Application{void feed_wdt(){last_feed=clock_ms;}} App;
namespace watchdog {
struct WatchdogManager {
 uint32_t saved;
 explicit WatchdogManager(uint32_t b):saved(budget){budget=b;}
 ~WatchdogManager(){assert(clock_ms-last_feed<saved);budget=saved;}
};
}
using esp_err_t=int;
constexpr int ESP_OK=0,ESP_ERR_OTA_VALIDATE_FAILED=1,ESP_ERR_FLASH_OP_TIMEOUT=2,ESP_ERR_FLASH_OP_FAIL=3;
enum OTAResponseTypes {OTA_RESPONSE_OK,OTA_RESPONSE_ERROR_MD5_MISMATCH,
 OTA_RESPONSE_ERROR_UPDATE_END,OTA_RESPONSE_ERROR_WRITING_FLASH,OTA_RESPONSE_ERROR_UNKNOWN};
struct Partition{size_t size=16515072;};
unsigned end_calls=0,boot_calls=0;int first_error=0,second_error=0;
void validation(){clock_ms+=20000;if(clock_ms-last_feed>=budget)std::exit(7);}
int esp_ota_end(int){++end_calls;validation();return first_error;}
int esp_ota_set_boot_partition(Partition*){++boot_calls;validation();return second_error;}
struct Md5{bool matches=true;void calculate(){}bool equals_hex(const char*){return matches;}};
struct IDFOTABackend {
 Partition partition;Partition *partition_=&partition;bool md5_set_=true;
 Md5 md5_;const char *expected_bin_md5_="digest";int update_handle_=7;bool aborted=false;
 void abort(){aborted=true;}
 OTAResponseTypes end();
};
'''
    checks = r'''
int main(){
 for(unsigned mode=0;mode<4;++mode) {
  clock_ms=last_feed=0;budget=5000;end_calls=boot_calls=0;
  first_error=mode==1 ? ESP_ERR_OTA_VALIDATE_FAILED : ESP_OK;
  second_error=mode==2 ? ESP_ERR_FLASH_OP_FAIL : ESP_OK;
  IDFOTABackend ota;ota.md5_.matches=mode!=3;
  auto result=ota.end();assert(budget==5000);
  if(mode==0){assert(result==OTA_RESPONSE_OK && end_calls==1 && boot_calls==1);}
  if(mode==1){assert(result==OTA_RESPONSE_ERROR_UPDATE_END && boot_calls==0);}
  if(mode==2){assert(result==OTA_RESPONSE_ERROR_WRITING_FLASH && boot_calls==1);}
  if(mode==3){assert(result==OTA_RESPONSE_ERROR_MD5_MISMATCH && ota.aborted && end_calls==0 && boot_calls==0);}
 }
}
'''
    cpp = tmp_path / "ota.cpp"
    cpp.write_text(harness + method + checks)
    exe = tmp_path / "ota"
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                    str(cpp), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
    # Without progress between the two individually valid phases, the same
    # watchdog budget expires. This is the original missing-boundary witness.
    cpp.write_text(harness + method.replace("App.feed_wdt();", "") + checks)
    subprocess.run(["g++", "-std=c++17", str(cpp), "-o", str(exe)], check=True)
    assert subprocess.run([str(exe)], capture_output=True).returncode == 7
