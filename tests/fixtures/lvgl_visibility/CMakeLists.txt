cmake_minimum_required(VERSION 3.20)
project(lvgl_visibility_contract LANGUAGES C CXX ASM)

if(NOT LVGL_SOURCE)
  message(FATAL_ERROR "LVGL_SOURCE must name the official managed LVGL source")
endif()
set(LV_BUILD_CONF_PATH "${CMAKE_CURRENT_SOURCE_DIR}/lv_conf.h" CACHE PATH "" FORCE)
set(CONFIG_LV_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(CONFIG_LV_BUILD_DEMOS OFF CACHE BOOL "" FORCE)
set(CONFIG_LV_USE_THORVG_INTERNAL OFF CACHE BOOL "" FORCE)
add_subdirectory("${LVGL_SOURCE}" lvgl)

add_executable(visibility_oracle runtime.cpp)
target_compile_features(visibility_oracle PRIVATE cxx_std_17)
target_compile_options(visibility_oracle PRIVATE -Wall -Wextra -Werror)
target_include_directories(visibility_oracle PRIVATE
  "${CMAKE_CURRENT_SOURCE_DIR}/../../../esphome/components/lvgl_visibility")
target_link_libraries(visibility_oracle PRIVATE lvgl::lvgl m)
