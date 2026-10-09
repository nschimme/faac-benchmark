# On-device FAAC encoder benchmark (ESP-IDF 5.5)

Separate project from `../firmware/` (the decoder benchmark). libfaac is referenced in place
(`FAAC_DIR`, default `/home/nschimme/gitprojects/faac`, `libfaac/*.c` minus `quantize_sse.c`);
`components/faac_lib/config.h` stands in for the meson-generated `config.h`. Driven by
`../esp32_enc_bench.py`; to build by hand:

    export IDF_TOOLS_PATH=$PWD/scripts/esp32/.toolchain/tools && . $PWD/scripts/esp32/.toolchain/esp-idf/export.sh
    cd scripts/esp32/firmware_enc
    idf.py -B build/esp32c6 -DSDKCONFIG=build/esp32c6/sdkconfig set-target esp32c6
    idf.py -B build/esp32c6 -DSDKCONFIG=build/esp32c6/sdkconfig build

Cache variables: `FAAC_DIR`, `FAAC_SBR_DECIMATION` (meson `sbr-decimation`, default 1),
`FAAC_MAX_CHANNELS` (meson `max-channels`, default 2; encoder state scales with it).

## Protocol (little-endian, packed; transport as in `../firmware/README.md`)

Host to device:

    "ENCB" | u32 sample_rate | u32 channels | u32 bit_rate | u32 object_type |
    u32 loops | u32 flags | u32 pcm_bytes
    ... wait for "ENCA" (or an early "ENCR" with a nonzero status), then:
    pcm_bytes of interleaved s16le PCM
    bit_rate is per channel (libfaac's unit); object_type 0 auto, 2 LC, 5 HE-AAC v1
    flags bit0 = return the ADTS bitstream of pass 1

Device to host (72 bytes):

    "ENCR" | u32 status | u32 frames | u32 out_bytes | u32 object_type | u32 frame_samples |
    u32 enc_rate | u32 bit_rate | u32 bandwidth | u64 best_cycles | u64 mean_cycles |
    u32 stream_crc32 | u32 heap_min_free_delta | u32 stack_hwm | u32 init_heap_used |
    u32 open_cycles
    then, if flags bit0 and status == 0: out_bytes of ADTS

- On status 2 and 6 `heap_min_free_delta` carries the largest free internal block and `init_heap_used`
  the room reserved for the encoder, to size `MAX_PCM_BYTES`.
- `status`: 0 ok, 1 bad request, 2 clip does not fit, 3 encoder open failed (the library rejected
  the configuration), 4 bitstream buffer overflow, 5 no frames, 6 out of memory, 7 encode failed.
- `object_type`, `frame_samples`, `enc_rate`, `bit_rate`, `bandwidth` are the resolved values from
  `faac_encoder_get_info`.
- `best_cycles` / `mean_cycles`: CPU cycles in `faac_encoder_encode` for one pass over the clip,
  including the end-of-stream flush calls (min and mean over `loops`). Encoder open/close and the
  copy of each input frame into internal RAM are outside the timed region.
- `out_bytes`, `frames`, `stream_crc32` (zlib CRC-32): ADTS output of pass 1.
- `init_heap_used`: internal heap taken by `faac_encoder_open`. `heap_min_free_delta`: free internal
  heap just before the timed passes minus the all-time minimum (the host reboots the board per clip).
- `stack_hwm`: free bytes at the low point of the 32 KB main task stack.
- `open_cycles`: cost of the pass-1 `faac_encoder_open`; the one-time table setup is already done
  by the sizing open, so this is the per-instance cost.

The ack exists because the device opens a probe encoder and sizes its buffers before reading the PCM,
which takes longer than the 16 KB UART receive buffer lasts at 921600 baud. The PCM is held in PSRAM when present, otherwise in internal RAM after leaving room for the
encoder state, which the firmware measures by opening one encoder before accepting the clip.
