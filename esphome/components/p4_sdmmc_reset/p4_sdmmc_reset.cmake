# ESP-IDF v5.5 SDMMC reset ordering fix, Espressif's 2026-10-27 proposal:
# https://github.com/espressif/esp-hosted-mcu/issues/240#issuecomment-6451429332
# Change only the per-build copy. Never modify IDF_PATH or replace its component.
include_guard(GLOBAL)

function(_esphome_p4_sdmmc_reset)
    idf_build_get_property(_target IDF_TARGET)
    if(NOT _target STREQUAL "esp32p4" OR BOOTLOADER_BUILD)
        message(STATUS "P4 SDMMC reset backport not applied: target ${_target}, bootloader=${BOOTLOADER_BUILD}")
        return()
    endif()
    idf_build_get_property(_idf_path IDF_PATH)
    include("${_idf_path}/tools/cmake/version.cmake")
    set(_version "${IDF_VERSION_MAJOR}.${IDF_VERSION_MINOR}.${IDF_VERSION_PATCH}")
    if(NOT _version STREQUAL "5.5.6")
        message(STATUS "P4 SDMMC reset backport not applied to SDK ${_version}: only exact SDK 5.5.6 is supported")
        return()
    endif()

    set(_original "${_idf_path}/components/esp_system/port/soc/esp32p4/system_internal.c")
    set(_copy "${CMAKE_BINARY_DIR}/p4_sdmmc_reset/system_internal.c")
    set(_before_sha "7bd2af3da2a9c1592c8d9ff6f65ad7fe320690b2e480076685df068f231e40cc")
    set(_after_sha "38aca3174d6ac05afa37c87e9c9d33533c3772fb136b17c44d6ee20bcdad4946")
    if(NOT EXISTS "${_original}")
        message(FATAL_ERROR "P4 SDMMC reset backport: SDK 5.5.6 source is missing; no replacement written")
    endif()
    file(SHA256 "${_original}" _sha)
    if(NOT _sha STREQUAL _before_sha)
        message(FATAL_ERROR "P4 SDMMC reset backport: unrecognized SDK 5.5.6 source SHA256 ${_sha}; no replacement written")
    endif()

    idf_component_get_property(_library esp_system COMPONENT_LIB)
    if(NOT TARGET "${_library}")
        message(FATAL_ERROR "P4 SDMMC reset backport: original esp_system target is missing")
    endif()
    get_target_property(_sources "${_library}" SOURCES)
    set(_count 0)
    set(_copies 0)
    foreach(_source IN LISTS _sources)
        if(_source STREQUAL _original)
            math(EXPR _count "${_count} + 1")
        elseif(_source STREQUAL _copy)
            math(EXPR _copies "${_copies} + 1")
        endif()
    endforeach()
    if(_count EQUAL 0 AND _copies EQUAL 1 AND EXISTS "${_copy}")
        file(SHA256 "${_copy}" _copy_sha)
        if(_copy_sha STREQUAL _after_sha)
            message(STATUS "P4 SDMMC reset backport already applied to the build-local source")
            return()
        endif()
    endif()
    if(NOT _count EQUAL 1 OR NOT _copies EQUAL 0)
        message(FATAL_ERROR "P4 SDMMC reset backport: expected exactly one original source, found ${_count}, copies=${_copies}; no replacement written")
    endif()

    file(READ "${_original}" _contents)
    set(_block [=[    // The DMA inside SDMMC Host needs to be reset to avoid memory corruption after restart.
    SET_PERI_REG_MASK(LP_CLKRST_HP_SDMMC_EMAC_RST_CTRL_REG, LP_CLKRST_RST_EN_SDMMC);
    CLEAR_PERI_REG_MASK(LP_CLKRST_HP_SDMMC_EMAC_RST_CTRL_REG, LP_CLKRST_RST_EN_SDMMC);

]=])
    set(_anchor "    // Set Peripheral clk rst\n")
    string(REPLACE "${_block}" "" _without "${_contents}")
    string(REPLACE "${_anchor}" "${_block}${_anchor}" _patched "${_without}")
    string(SHA256 _patched_sha "${_patched}")
    if(NOT _patched_sha STREQUAL _after_sha)
        message(FATAL_ERROR "P4 SDMMC reset backport: exact reset-order transformation failed; no replacement written")
    endif()

    set(_copy_sha "")
    if(EXISTS "${_copy}")
        file(SHA256 "${_copy}" _copy_sha)
    endif()
    if(NOT _copy_sha STREQUAL _after_sha)
        file(MAKE_DIRECTORY "${CMAKE_BINARY_DIR}/p4_sdmmc_reset")
        file(WRITE "${_copy}" "${_patched}")
    endif()

    # Keep the same component target, include/compile options and linker.lf.
    # Preserve per-source properties in the directory that owns that target.
    foreach(_property COMPILE_DEFINITIONS COMPILE_OPTIONS COMPILE_FLAGS
                      INCLUDE_DIRECTORIES LANGUAGE OBJECT_DEPENDS OBJECT_OUTPUTS
                      HEADER_FILE_ONLY SKIP_PRECOMPILE_HEADERS SKIP_UNITY_BUILD_INCLUSION)
        get_source_file_property(_value "${_original}" TARGET_DIRECTORY "${_library}" "${_property}")
        if(NOT _value STREQUAL "NOTFOUND")
            set_source_files_properties("${_copy}" TARGET_DIRECTORY "${_library}"
                PROPERTIES "${_property}" "${_value}")
        endif()
    endforeach()
    # Retain the basename system_internal.c for esp_system/linker.lf's IRAM mappings.
    list(FIND _sources "${_original}" _index)
    list(REMOVE_AT _sources ${_index})
    list(INSERT _sources ${_index} "${_copy}")
    set_property(TARGET "${_library}" PROPERTY SOURCES "${_sources}")
    message(STATUS "P4 SDMMC reset backport applied to build-local system_internal.c, SDK 5.5.6, SHA256 ${_after_sha}")
endfunction()

# ESP-IDF's project() creates component targets after the CMake project hook.
# Run after those targets exist, in the original top-level directory.
cmake_language(DEFER DIRECTORY "${CMAKE_SOURCE_DIR}" ID esphome_p4_sdmmc_reset
    CALL _esphome_p4_sdmmc_reset)
