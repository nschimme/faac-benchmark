foreach(v CODEC CODEC_PSRAM CODEC_SBR CODEC_HELIX_DIR CODEC_FAAD2_DIR)
    set(${v} "$ENV{AAC_${v}}")
endforeach()

if(NOT CMAKE_BUILD_EARLY_EXPANSION)
    file(GLOB helix_srcs "${CODEC_HELIX_DIR}/*.c")
    if(NOT helix_srcs)
        message(FATAL_ERROR "No Helix sources in CODEC_HELIX_DIR='${CODEC_HELIX_DIR}'")
    endif()
endif()

idf_component_register(SRCS "codec_helix.c" ${helix_srcs}
                       INCLUDE_DIRS "."
                       PRIV_INCLUDE_DIRS "shim" "${CODEC_HELIX_DIR}"
                       REQUIRES codec_api heap)

target_compile_definitions(${COMPONENT_LIB} PRIVATE
    USE_DEFAULT_STDLIB HELIX_LOGGING_ACTIVE=0)
if(CODEC_SBR STREQUAL "1")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE HELIX_FEATURE_AUDIO_CODEC_AAC_SBR)
endif()
target_compile_options(${COMPONENT_LIB} PRIVATE -w)

if(CODEC_PSRAM STREQUAL "1")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE CODEC_PSRAM_STATE)
endif()
