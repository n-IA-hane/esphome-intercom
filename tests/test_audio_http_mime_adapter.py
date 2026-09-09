"""Build the IDF compatibility adapter with and without upstream WAV support."""

from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "esphome/components/audio_http/idf_components/micro_decoder_mime/CMakeLists.txt"


@pytest.mark.parametrize("wav", [False, True])
@pytest.mark.parametrize("upstream_fixed", [False, True])
def test_mime_adapter_preserves_source_and_replaces_only_one_unit(tmp_path, wav, upstream_fixed):
    source_dir = tmp_path / "decoder/src"
    source_dir.mkdir(parents=True)
    extra = ' || contains("audio/vnd.wave")' if upstream_fixed else ''
    original = '''#include <cstring>
bool detects(const char *mime) {
#ifdef MICRO_DECODER_CODEC_WAV
 auto contains = [mime](const char *v) { return std::strstr(mime,v)!=nullptr; };
 return contains("audio/wave")EXTRA;
#else
 (void)mime; return false;
#endif
}
'''.replace("EXTRA", extra)
    (source_dir / "types.cpp").write_text(original)
    (tmp_path / "main.cpp").write_text(f'''
#include <cassert>
bool detects(const char*);
int main() {{
 assert(detects("audio/vnd.wave; codec=1")=={str(wav).lower()});
 assert(detects("audio/wave")=={str(wav).lower()});
 assert(!detects("application/json"));
}}
''')
    (tmp_path / "CMakeLists.txt").write_text(f'''
cmake_minimum_required(VERSION 3.20)
project(mime_adapter LANGUAGES CXX)
set(CONFIG_MICRO_DECODER_CODEC_WAV {"ON" if wav else "OFF"})
add_library(decoder decoder/src/types.cpp)
if(CONFIG_MICRO_DECODER_CODEC_WAV)
 target_compile_definitions(decoder PRIVATE MICRO_DECODER_CODEC_WAV)
endif()
function(idf_component_register)
endfunction()
function(idf_component_get_property result component property)
 if(property STREQUAL "COMPONENT_DIR")
  set(${{result}} "${{CMAKE_CURRENT_SOURCE_DIR}}/decoder" PARENT_SCOPE)
 else()
  set(${{result}} decoder PARENT_SCOPE)
 endif()
endfunction()
include("{ADAPTER}")
get_target_property(final_sources decoder SOURCES)
list(LENGTH final_sources count)
if(NOT count EQUAL 1)
 message(FATAL_ERROR "The adapter added a parallel detector")
endif()
add_executable(probe main.cpp)
target_link_libraries(probe PRIVATE decoder)
''')
    build = tmp_path / "build"
    subprocess.run(["cmake", "-S", str(tmp_path), "-B", str(build)], check=True, capture_output=True)
    subprocess.run(["cmake", "--build", str(build)], check=True, capture_output=True)
    subprocess.run([str(build / "probe")], check=True)
    assert (source_dir / "types.cpp").read_text() == original
    assert (build / "micro_decoder_types.cpp").exists() is (wav and not upstream_fixed)
