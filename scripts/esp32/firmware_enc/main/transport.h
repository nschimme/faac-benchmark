#pragma once
#include <stddef.h>

/* Binary byte pipe to the host. ESP32 uses UART0; targets with a built-in
 * USB-Serial/JTAG peripheral (esp32s3, esp32c6) use that instead. */
int transport_init(void);                       /* 0 ok */
int transport_read(void *buf, size_t n);        /* blocks until n bytes; 0 ok */
int transport_write(const void *buf, size_t n); /* 0 ok */
