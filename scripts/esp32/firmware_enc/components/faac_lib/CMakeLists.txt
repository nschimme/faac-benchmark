foreach(v FAAC_DIR FAAC_SBR_DECIMATION FAAC_MAX_CHANNELS)
    set(${v} "$ENV{ENC_${v}}")
endforeach()

if(NOT CMAKE_BUILD_EARLY_EXPANSION)
    file(GLOB faac_srcs "${FAAC_DIR}/libfaac/*.c")
    # The SSE2 quantizer is x86 only; the portable one is in quantize.c.
    list(FILTER faac_srcs EXCLUDE REGEX "quantize_sse\\.c$")
    if(NOT faac_srcs)
        message(FATAL_ERROR "No libfaac sources in FAAC_DIR='${FAAC_DIR}'")
    endif()
endif()

idf_component_register(SRCS ${faac_srcs}
                       INCLUDE_DIRS "${FAAC_DIR}/include"
                       PRIV_INCLUDE_DIRS "${FAAC_DIR}/libfaac" "${CMAKE_CURRENT_LIST_DIR}")

# meson force-includes a generated config.h into every file; this is its equivalent.
target_compile_definitions(${COMPONENT_LIB} PRIVATE
    FAAC_SBR_DECIMATION=${FAAC_SBR_DECIMATION} MAX_CHANNELS=${FAAC_MAX_CHANNELS})
target_compile_options(${COMPONENT_LIB} PRIVATE -include "${CMAKE_CURRENT_LIST_DIR}/config.h" -w)
