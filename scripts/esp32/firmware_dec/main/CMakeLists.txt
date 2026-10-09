foreach(v CODEC CODEC_SBR CODEC_HELIX_DIR CODEC_FAAD2_DIR)
    set(${v} "$ENV{AAC_${v}}")
endforeach()

set(transport_req esp_driver_uart)

idf_component_register(SRCS "bench_main.c" "transport.c"
                       INCLUDE_DIRS "."
                       REQUIRES codec_api codec_${CODEC} ${transport_req}
                       PRIV_REQUIRES esp_driver_uart esp_timer heap freertos esp_hw_support)

target_compile_definitions(${COMPONENT_LIB} PRIVATE
    CODEC_NAME=\"${CODEC}\" CODEC_SBR=${CODEC_SBR})
