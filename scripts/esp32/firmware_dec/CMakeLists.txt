cmake_minimum_required(VERSION 3.16)

# Build-time switches (pass with idf.py -DNAME=value).
set(CODEC "helix" CACHE STRING "AAC decoder to benchmark: helix | faad2 | faad3")
set(CODEC_SBR "1" CACHE STRING "1 = SBR (HE-AAC v1) enabled, 0 = AAC-LC only")
set(CODEC_PSRAM "0" CACHE STRING "1 = decoder state allocated from PSRAM instead of internal RAM (S3 only)")
set(CODEC_PS "0" CACHE STRING "faad3 only: 1 = also build Parametric Stereo")
set(CODEC_FAAD3_OPEN "0" CACHE STRING "faad3 only: 1 = faad_decoder_open (split allocations) instead of one caller-owned block")
set(CODEC_HELIX_DIR "/home/nschimme/gitprojects/codec-helix/src/libhelix-aac"
    CACHE PATH "Helix libhelix-aac source directory")
set(CODEC_FAAD2_DIR "/home/nschimme/gitprojects/faad2"
    CACHE PATH "FAAD2 checkout root (contains libfaad/ and include/)")
set(CODEC_FAAD3_DIR "${CMAKE_CURRENT_LIST_DIR}/../.toolchain/faad3-src"
    CACHE PATH "FAAD3 snapshot made by scripts/esp32/fetch_faad3.sh")

if(NOT CODEC MATCHES "^(helix|faad2|faad3)$")
    message(FATAL_ERROR "CODEC must be helix, faad2 or faad3 (got '${CODEC}')")
endif()

# Component CMakeLists also run in IDF's cache-less early-expansion pass, so
# hand the switches over through the environment as well.
foreach(v CODEC CODEC_SBR CODEC_PS CODEC_FAAD3_OPEN CODEC_PSRAM CODEC_HELIX_DIR CODEC_FAAD2_DIR CODEC_FAAD3_DIR)
    set(ENV{AAC_${v}} "${${v}}")
endforeach()

# Only the selected adapter component takes part in the build.
set(EXCLUDE_COMPONENTS codec_helix codec_faad2 codec_faad3)
list(REMOVE_ITEM EXCLUDE_COMPONENTS codec_${CODEC})

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(aac_bench)
