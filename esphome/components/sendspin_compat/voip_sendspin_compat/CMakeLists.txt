idf_component_register(REQUIRES sendspin__sendspin-cpp)
idf_component_get_property(sendspin_library sendspin__sendspin-cpp COMPONENT_LIB)
idf_component_get_property(sendspin_dir sendspin__sendspin-cpp COMPONENT_DIR)
file(READ "${sendspin_dir}/src/sync_task.cpp" sync_source)
set(early_discard [=[        if (client_timestamp < sync_context.new_audio_client_playtime - HARD_SYNC_THRESHOLD_US) {
            // This chunk will arrive too late to be played, skip it!
            this->encoded_ring_buffer_->return_chunk(sync_context.encoded_entry);
            sync_context.encoded_entry = nullptr;
            return DecodeResult::SKIPPED;
        }
]=])
string(FIND "${sync_source}" "${early_discard}" early_discard_position)
if(early_discard_position EQUAL -1)
  message(FATAL_ERROR "Sendspin compatibility adapter requires review for this SDK source")
endif()
string(REPLACE "${early_discard}" "" sync_source "${sync_source}")
file(READ "${CMAKE_CURRENT_LIST_DIR}/thread_config.h" thread_config_source)
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/thread_config.h" "${thread_config_source}")
get_target_property(sendspin_sources ${sendspin_library} SOURCES)
set(original_sendspin_sources ${sendspin_sources})
foreach(role IN ITEMS sync_task artwork_role visualizer_role)
  set(role_sources ${original_sendspin_sources})
  list(FILTER role_sources INCLUDE REGEX "/${role}\\.cpp$")
  if(role_sources)
    if(role STREQUAL "sync_task")
      set(role_source "${sync_source}")
    else()
      file(READ "${sendspin_dir}/src/${role}.cpp" role_source)
    endif()
    string(REPLACE "#include \"platform/thread.h\"" "#include \"thread_config.h\"" role_source "${role_source}")
    string(FIND "${role_source}" "platform_configure_thread(" thread_setup_position)
    if(thread_setup_position EQUAL -1)
      message(FATAL_ERROR "Sendspin ${role} thread configuration changed; review compatibility adapter")
    endif()
    string(REGEX REPLACE "platform_configure_thread\\(([^;]+)\\);"
      "PlatformThreadConfig thread_config(\\1);\n    if (!thread_config.valid()) {\n        SS_LOGE(TAG, \"Thread configuration rejected\");\n        return false;\n    }"
      role_source "${role_source}")
    file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/${role}_compat.cpp" "${role_source}")
    list(FILTER sendspin_sources EXCLUDE REGEX "/${role}\\.cpp$")
    list(APPEND sendspin_sources "${CMAKE_CURRENT_BINARY_DIR}/${role}_compat.cpp")
  endif()
endforeach()
set_property(TARGET ${sendspin_library} PROPERTY SOURCES "${sendspin_sources}")
