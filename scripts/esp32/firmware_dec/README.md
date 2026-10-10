# On-device AAC decoder benchmark (ESP-IDF 5.5)

Measures decode cycles, heap and stack use of Helix or FAAD2 on esp32, esp32s3
and esp32c6. Decoder sources are referenced in place, never copied.

## Build and flash

Activate the toolchain as `setup_idf.sh` prints, from the repository root:

    export IDF_TOOLS_PATH=$PWD/scripts/esp32/.toolchain/tools && . $PWD/scripts/esp32/.toolchain/esp-idf/export.sh
    cd scripts/esp32/firmware_dec

    # one build directory per codec/target; sdkconfig is per build dir too
    idf.py -B build/helix-esp32s3 -DSDKCONFIG=build/helix-esp32s3/sdkconfig \
        -DCODEC=helix -DCODEC_SBR=1 set-target esp32s3
    idf.py -B build/helix-esp32s3 -DSDKCONFIG=build/helix-esp32s3/sdkconfig build
    idf.py -B build/helix-esp32s3 -DSDKCONFIG=build/helix-esp32s3/sdkconfig -p /dev/ttyACM0 flash

    # FAAD2, AAC-LC only, on the classic ESP32 (UART0 via the board's USB bridge)
    idf.py -B build/faad2-esp32 -DSDKCONFIG=build/faad2-esp32/sdkconfig \
        -DCODEC=faad2 -DCODEC_SBR=0 set-target esp32
    idf.py -B build/faad2-esp32 -DSDKCONFIG=build/faad2-esp32/sdkconfig build
    idf.py -B build/faad2-esp32 -DSDKCONFIG=build/faad2-esp32/sdkconfig -p /dev/ttyUSB0 flash

Switches (cache variables): `CODEC=helix|faad2` (default helix),
`CODEC_SBR=1|0` (default 1), `CODEC_HELIX_DIR` (default
`/home/nschimme/gitprojects/codec-helix/src/libhelix-aac`), `CODEC_FAAD2_DIR`
(default `/home/nschimme/gitprojects/faad2`). Changing `CODEC` needs a fresh
build directory (as above) or `fullclean`.

Do not use `idf.py monitor` while the host harness owns the port.

## Transport

| target  | port                                   |
|---------|----------------------------------------|
| all targets | UART0, 921600 8N1 (`BENCH_UART_BAUD`), via the board's USB-UART bridge |

The boot ROM and one ESP_LOGI line appear on the same port before the first
request; the host must discard bytes until it sees `AACR`. Logging is switched
off after that line.

## Protocol (little-endian, packed)

Host to device:

    "AACB" | u32 clip_len | u32 loops | u32 flags | clip_len bytes of ADTS
    flags bit0 = return decoded PCM

Device to host:

    "AACR" | u32 status | u32 frames | u32 samples_out | u32 out_rate |
    u32 out_channels | u64 best_cycles | u64 mean_cycles | u32 pcm_crc32 |
    u32 heap_min_free_delta | u32 stack_hwm | u32 init_heap_used
    then, if flags bit0 and status == 0: samples_out * 2 bytes of int16 PCM

Total header: 56 bytes.

### Streaming request (flags bit1)

With `flags` bit1 set the clip is not sent with the request (`clip_len` is only its length); the device keeps a 16 KB
window in internal RAM and pulls the clip in blocks, so no chip RAM ceiling applies to the clip (the harness still
needs the 16 KB window, which is excluded from the heap figures). The exchange, all little-endian:

    device -> host: "AACQ" | u32 offset | u32 n          host answers with exactly n bytes of the clip from offset
    device -> host: "AACD" | u32 bytes | PCM              flags bit0 only: decoded PCM of pass 1, as it is produced
    device -> host: "AACR" | 56-byte header              end; no PCM follows it

The device refills the window outside the timed region (between decode calls), so cycle counts measure the same
thing as the resident mode; each pass fetches the clip again. `esp32_dec_bench.py run --stream auto` uses it on the
targets without PSRAM. Field notes:

- `status`: 0 ok, 1 bad request, 2 clip does not fit, 3 codec init failed,
  4 PCM return buffer overflow, 5 no frame decoded, 6 out of memory.
- `samples_out`: interleaved int16 values (frames x channels x samples) of pass 1.
- `best_cycles` / `mean_cycles`: CPU cycles spent in `codec_decode_frame` for one
  pass over the clip (min and mean over `loops`). The decoder is closed and
  reopened between passes, outside the timed region.
- `pcm_crc32`: standard CRC-32 (zlib) of the PCM bytes of the first pass.
- `heap_min_free_delta`: free internal heap before `codec_init` minus the
  all-time internal minimum free size (the watermark is not reset between
  requests, so it can include a larger earlier request).
- `stack_hwm`: `uxTaskGetStackHighWaterMark` of the main task, in bytes still
  free at the low point (stack size 65536; FAAD2 alone needs ~46 KB).
- `init_heap_used`: internal heap consumed by `codec_init`.

The clip is held in PSRAM when present (esp32s3), otherwise in internal RAM
with 64 KB reserved; oversized clips are drained and answered with status 2.
The host should send at a pace the 16 KB receive buffer tolerates (chunks of
a few KB are fine; UART0 has no flow control, so the host paces writes in 4 KB chunks).
