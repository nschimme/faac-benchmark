foreach(v CODEC CODEC_PSRAM CODEC_SBR CODEC_PS CODEC_FAAD3_OPEN CODEC_FAAD3_DIR)
    set(${v} "$ENV{AAC_${v}}")
endforeach()

if(NOT CMAKE_BUILD_EARLY_EXPANSION)
    file(GLOB faad3_srcs "${CODEC_FAAD3_DIR}/libfaad/*.c")
    list(FILTER faad3_srcs EXCLUDE REGEX "/test_[^/]*\\.c$")
    if(NOT CODEC_SBR STREQUAL "1")
        list(FILTER faad3_srcs EXCLUDE REGEX "/sbr_dec_tables\\.c$")
    endif()
    list(APPEND faad3_srcs "${CODEC_FAAD3_DIR}/common/fft.c"
                           "${CODEC_FAAD3_DIR}/common/sbr_tables.c"
                           "${CODEC_FAAD3_DIR}/common/sfb_tables.c")
    if(NOT EXISTS "${CODEC_FAAD3_DIR}/libfaad/decoder.c")
        message(FATAL_ERROR "No FAAD3 sources in CODEC_FAAD3_DIR='${CODEC_FAAD3_DIR}'; run scripts/esp32/fetch_faad3.sh")
    endif()
endif()

idf_component_register(SRCS "codec_faad3.c" ${faad3_srcs}
                       INCLUDE_DIRS "."
                       PRIV_INCLUDE_DIRS "shim" "${CODEC_FAAD3_DIR}" "${CODEC_FAAD3_DIR}/include"
                                         "${CODEC_FAAD3_DIR}/common" "${CODEC_FAAD3_DIR}/libfaad"
                       REQUIRES codec_api heap)

# Same switches as libfaad/meson.build; the config.h shim is forced into every TU like Meson does.
target_compile_definitions(${COMPONENT_LIB} PRIVATE COMMON_PREFIX=faad_ FAAD_STATIC)
target_compile_options(${COMPONENT_LIB} PRIVATE -w -include config.h)
if(NOT CODEC_SBR STREQUAL "1")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FAAD_DISABLE_SBR)
endif()
if(NOT CODEC_PS STREQUAL "1" OR NOT CODEC_SBR STREQUAL "1")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FAAD_DISABLE_PS)
endif()

if(CODEC_PSRAM STREQUAL "1")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE CODEC_PSRAM_STATE)
endif()

if(CODEC_FAAD3_OPEN STREQUAL "1")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE CODEC_FAAD3_OPEN)
endif()
