cmake_minimum_required(VERSION 3.16)

# Build-time switches (pass with idf.py -DNAME=value).
set(FAAC_DIR "/home/nschimme/gitprojects/faac"
    CACHE PATH "FAAC checkout root (contains libfaac/ and include/)")
set(FAAC_SBR_DECIMATION "1" CACHE STRING "libfaac sbr-decimation meson option (1..8)")
set(FAAC_MAX_CHANNELS "2" CACHE STRING "libfaac max-channels meson option; state scales with it")

# Component CMakeLists also run in IDF's cache-less early-expansion pass.
foreach(v FAAC_DIR FAAC_SBR_DECIMATION FAAC_MAX_CHANNELS)
    set(ENV{ENC_${v}} "${${v}}")
endforeach()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(aac_enc_bench)
