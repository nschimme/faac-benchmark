foreach(v CODEC CODEC_PSRAM CODEC_SBR CODEC_HELIX_DIR CODEC_FAAD2_DIR)
    set(${v} "$ENV{AAC_${v}}")
endforeach()

if(NOT CMAKE_BUILD_EARLY_EXPANSION)
    file(GLOB faad_srcs "${CODEC_FAAD2_DIR}/libfaad/*.c")
    if(NOT faad_srcs)
        message(FATAL_ERROR "No FAAD2 sources in CODEC_FAAD2_DIR='${CODEC_FAAD2_DIR}'")
    endif()
endif()

idf_component_register(SRCS "codec_faad2.c" ${faad_srcs}
                       INCLUDE_DIRS "."
                       PRIV_INCLUDE_DIRS "${CODEC_FAAD2_DIR}/libfaad" "${CODEC_FAAD2_DIR}/include"
                       REQUIRES codec_api heap)

# Same predefined config as faad2/CMakeLists.txt (FAAD_DEFINES), minus
# APPLY_DRC and HAVE_LRINTF (not needed for the fixed-point LC path).
# FIXED_POINT: integer decoder. LC_ONLY_DECODER: undefines MAIN/LTP/LD/SSR/
# ERROR_RESILIENCE in common.h but leaves SBR_DEC alone.
target_compile_definitions(${COMPONENT_LIB} PRIVATE
    FIXED_POINT LC_ONLY_DECODER
    HAVE_INTTYPES_H=1 HAVE_MEMCPY=1 HAVE_STRING_H=1 HAVE_STRINGS_H=1
    HAVE_SYS_STAT_H=1 HAVE_SYS_TYPES_H=1
    PACKAGE_VERSION=\"faad2-bench\")
if(CODEC_SBR STREQUAL "1")
    # Upstream's fixed-point + SBR_LOW_POWER combination does not compile
    # (mask / SBR_SQRT_Q2), so SBR builds keep PS_DEC. Streams here are HE-AAC
    # v1, so PS is never exercised, but its code counts toward footprint.
else()
    target_compile_definitions(${COMPONENT_LIB} PRIVATE DISABLE_SBR)
endif()
target_compile_options(${COMPONENT_LIB} PRIVATE -w)

if(CODEC_PSRAM STREQUAL "1")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE CODEC_PSRAM_STATE)
endif()

# FAAD2 allocates only through faad_malloc/faad_free; rename the originals in common.c and
# provide the real ones in the adapter so placement follows CODEC_PSRAM like the other codecs.
set_source_files_properties("${CODEC_FAAD2_DIR}/libfaad/common.c" PROPERTIES
                            COMPILE_DEFINITIONS "faad_malloc=faad_malloc_orig;faad_free=faad_free_orig")
