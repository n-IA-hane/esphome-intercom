# Keep MIME classification in micro-decoder's existing detector. RFC 2361
# registers audio/vnd.wave, which micro-decoder 0.4.0 does not recognize.
idf_component_register(REQUIRES esphome__micro-decoder)

if(CMAKE_BUILD_EARLY_EXPANSION OR NOT CONFIG_MICRO_DECODER_CODEC_WAV)
    return()
endif()

idf_component_get_property(decoder_dir esphome__micro-decoder COMPONENT_DIR)
idf_component_get_property(decoder_lib esphome__micro-decoder COMPONENT_LIB)
file(READ "${decoder_dir}/src/types.cpp" detector)

# An upstream release that already supports the MIME type needs no adaptation.
if(detector MATCHES "audio/vnd.wave")
    return()
endif()
set(needle "contains(\"audio/wave\")")
string(FIND "${detector}" "${needle}" location)
if(location EQUAL -1)
    message(FATAL_ERROR "micro-decoder WAV detector changed; review MIME compatibility adapter")
endif()
string(REPLACE "${needle}" "${needle} || contains(\"audio/vnd.wave\")" detector "${detector}")
set(patched "${CMAKE_CURRENT_BINARY_DIR}/micro_decoder_types.cpp")
file(WRITE "${patched}" "${detector}")

# Replace the compilation unit, not the downloaded source or the decoder API.
# There is still exactly one detector and no extra runtime buffer or task.
get_target_property(sources ${decoder_lib} SOURCES)
list(FILTER sources EXCLUDE REGEX "(^|/)types\\.cpp$")
list(APPEND sources "${patched}")
set_property(TARGET ${decoder_lib} PROPERTY SOURCES "${sources}")
