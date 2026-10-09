set(transport_req esp_driver_uart)

idf_component_register(SRCS "enc_main.c" "transport.c"
                       INCLUDE_DIRS "."
                       REQUIRES faac_lib ${transport_req}
                       PRIV_REQUIRES esp_driver_uart esp_timer heap freertos esp_hw_support)
