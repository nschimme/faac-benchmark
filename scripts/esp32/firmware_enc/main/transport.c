#include "transport.h"
#include <stdint.h>
#include "sdkconfig.h"
#include "freertos/FreeRTOS.h"

#define RX_BUF_BYTES 16384
#define TX_BUF_BYTES 4096

#ifndef BENCH_USB_JTAG

#include "driver/uart.h"

#ifndef BENCH_UART_BAUD
#define BENCH_UART_BAUD 921600 /* must match CONFIG_ESP_CONSOLE_UART_BAUDRATE */
#endif
#define BENCH_UART UART_NUM_0

int transport_init(void)
{
    const uart_config_t cfg = {
        .baud_rate = BENCH_UART_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    if (uart_param_config(BENCH_UART, &cfg) != ESP_OK)
        return -1;
    if (uart_set_pin(BENCH_UART, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE,
                     UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE) != ESP_OK)
        return -1;
    return uart_driver_install(BENCH_UART, RX_BUF_BYTES, TX_BUF_BYTES, 0, NULL, 0) == ESP_OK ? 0 : -1;
}

int transport_read(void *buf, size_t n)
{
    uint8_t *p = buf;
    while (n) {
        int r = uart_read_bytes(BENCH_UART, p, n > 4096 ? 4096 : n, pdMS_TO_TICKS(1000));
        if (r < 0)
            return -1;
        p += r;
        n -= (size_t)r;
    }
    return 0;
}

int transport_write(const void *buf, size_t n)
{
    if (uart_write_bytes(BENCH_UART, buf, n) < 0)
        return -1;
    return uart_wait_tx_done(BENCH_UART, portMAX_DELAY) == ESP_OK ? 0 : -1;
}

#else /* USB-Serial/JTAG */

#include "driver/usb_serial_jtag.h"

int transport_init(void)
{
    usb_serial_jtag_driver_config_t cfg = {
        .tx_buffer_size = TX_BUF_BYTES,
        .rx_buffer_size = RX_BUF_BYTES,
    };
    return usb_serial_jtag_driver_install(&cfg) == ESP_OK ? 0 : -1;
}

int transport_read(void *buf, size_t n)
{
    uint8_t *p = buf;
    while (n) {
        int r = usb_serial_jtag_read_bytes(p, n > 4096 ? 4096 : n, pdMS_TO_TICKS(1000));
        if (r < 0)
            return -1;
        p += r;
        n -= (size_t)r;
    }
    return 0;
}

int transport_write(const void *buf, size_t n)
{
    const uint8_t *p = buf;
    int stalls = 0;
    while (n) {
        int w = usb_serial_jtag_write_bytes(p, n > 4096 ? 4096 : n, pdMS_TO_TICKS(100));
        if (w < 0)
            return -1;
        if (w == 0) {
            if (++stalls > 100) /* ~10 s without the host draining */
                return -1;
            continue;
        }
        stalls = 0;
        p += w;
        n -= (size_t)w;
    }
    return 0;
}

#endif
