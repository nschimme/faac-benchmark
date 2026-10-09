/* On-device AAC decoder benchmark. Protocol is described in ../README.md. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_cpu.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "codec_api.h"
#include "transport.h"

#define TAG "aacbench"

enum {
    ST_OK = 0,
    ST_BAD_REQUEST = 1,
    ST_CLIP_TOO_BIG = 2,
    ST_CODEC_INIT = 3,
    ST_PCM_OVERFLOW = 4,
    ST_NO_FRAMES = 5,
    ST_NO_MEMORY = 6,
};

#define FLAG_RETURN_PCM 1u
#define INTERNAL_RESERVE (64 * 1024) /* internal heap kept free when the clip lives in DRAM */

typedef struct __attribute__((packed)) {
    char magic[4];
    uint32_t status;
    uint32_t frames;
    uint32_t samples_out;
    uint32_t out_rate;
    uint32_t out_channels;
    uint64_t best_cycles;
    uint64_t mean_cycles;
    uint32_t pcm_crc32;
    uint32_t heap_min_free_delta;
    uint32_t stack_hwm;
    uint32_t init_heap_used;
} result_t;

_Static_assert(sizeof(result_t) == 56, "result_t must be packed to 56 bytes");

static int16_t *s_pcm; /* reused per-frame output, internal RAM */

static uint32_t crc_table[16];

static void crc_init(void)
{
    for (uint32_t i = 0; i < 16; i++) {
        uint32_t c = i;
        for (int k = 0; k < 4; k++)
            c = (c & 1) ? (c >> 1) ^ 0xEDB88320u : c >> 1;
        crc_table[i] = c;
    }
}

static uint32_t crc_update(uint32_t crc, const uint8_t *p, size_t n)
{
    while (n--) {
        crc ^= *p++;
        crc = (crc >> 4) ^ crc_table[crc & 15];
        crc = (crc >> 4) ^ crc_table[crc & 15];
    }
    return crc;
}

static int wait_magic(const char *m)
{
    uint32_t want = (uint32_t)(uint8_t)m[0] | (uint32_t)(uint8_t)m[1] << 8 |
                    (uint32_t)(uint8_t)m[2] << 16 | (uint32_t)(uint8_t)m[3] << 24;
    uint32_t win = 0;
    for (;;) {
        uint8_t b;
        if (transport_read(&b, 1) != 0)
            return -1;
        win = (win >> 8) | ((uint32_t)b << 24);
        if (win == want)
            return 0;
    }
}

static void drain(size_t n)
{
    while (n) {
        size_t c = n > CODEC_PCM_MAX_SAMPLES * 2 ? CODEC_PCM_MAX_SAMPLES * 2 : n;
        if (transport_read(s_pcm, c) != 0)
            return;
        n -= c;
    }
}

static int have_psram(void)
{
    return heap_caps_get_total_size(MALLOC_CAP_SPIRAM) > 0;
}

static void send_result(result_t *r, const uint8_t *pcm, size_t pcm_bytes)
{
    memcpy(r->magic, "AACR", 4);
    transport_write(r, sizeof(*r));
    if (pcm && pcm_bytes)
        transport_write(pcm, pcm_bytes);
}

static void serve_request(void)
{
    uint32_t hdr[3];
    result_t r;
    memset(&r, 0, sizeof(r));

    if (transport_read(hdr, sizeof(hdr)) != 0)
        return;
    uint32_t clip_len = hdr[0], loops = hdr[1], flags = hdr[2];

    if (clip_len == 0 || loops == 0) {
        r.status = ST_BAD_REQUEST;
        send_result(&r, NULL, 0);
        return;
    }

    /* Clip storage: PSRAM when present, internal RAM otherwise. */
    uint8_t *clip = NULL;
    if (have_psram()) {
        clip = heap_caps_malloc(clip_len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    } else {
        size_t largest = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        if (largest > INTERNAL_RESERVE && clip_len <= largest - INTERNAL_RESERVE)
            clip = heap_caps_malloc(clip_len, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    }
    if (!clip) {
        drain(clip_len); /* keep the stream in sync */
        r.status = ST_CLIP_TOO_BIG;
        send_result(&r, NULL, 0);
        return;
    }
    if (transport_read(clip, clip_len) != 0) {
        heap_caps_free(clip);
        return;
    }

    size_t base_free = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    if (codec_init() != 0) {
        heap_caps_free(clip);
        r.status = ST_CODEC_INIT;
        send_result(&r, NULL, 0);
        return;
    }
    size_t after_init = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    r.init_heap_used = base_free > after_init ? (uint32_t)(base_free - after_init) : 0;

    /* PCM return buffer: whatever is left in PSRAM (or internal RAM). */
    uint8_t *pcm_store = NULL;
    size_t pcm_cap = 0;
    if (flags & FLAG_RETURN_PCM) {
        if (have_psram()) {
            pcm_cap = heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM);
            if (pcm_cap)
                pcm_store = heap_caps_malloc(pcm_cap, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        } else {
            size_t largest = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
            if (largest > INTERNAL_RESERVE) {
                pcm_cap = largest - INTERNAL_RESERVE;
                pcm_store = heap_caps_malloc(pcm_cap, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
            }
        }
        if (!pcm_store) {
            codec_close();
            heap_caps_free(clip);
            r.status = ST_NO_MEMORY;
            send_result(&r, NULL, 0);
            return;
        }
    }

    uint32_t status = ST_OK;
    uint64_t best = UINT64_MAX, total = 0;
    uint32_t crc = 0xFFFFFFFFu;
    size_t pcm_used = 0;
    uint32_t frames = 0, samples_out = 0;
    int out_rate = 0, out_channels = 0;

    for (uint32_t pass = 0; pass < loops; pass++) {
        if (pass > 0) { /* identical decoder state for every pass */
            codec_close();
            if (codec_init() != 0) {
                status = ST_CODEC_INIT;
                break;
            }
        }
        const uint8_t *p = clip;
        int left = (int)clip_len;
        uint64_t cycles = 0;

        while (left > 0) {
            const uint8_t *before = p;
            codec_frame_info_t fi = {0};

            uint32_t t0 = esp_cpu_get_cycle_count();
            int rc = codec_decode_frame(&p, &left, s_pcm, &fi);
            uint32_t t1 = esp_cpu_get_cycle_count();
            cycles += (uint32_t)(t1 - t0); /* per-call delta survives counter wrap */

            if (rc == 0 && pass == 0) {
                size_t bytes = (size_t)fi.out_samples * sizeof(int16_t);
                crc = crc_update(crc, (const uint8_t *)s_pcm, bytes);
                frames++;
                samples_out += (uint32_t)fi.out_samples;
                out_rate = fi.out_rate;
                out_channels = fi.out_channels;
                if (pcm_store) {
                    if (pcm_used + bytes <= pcm_cap) {
                        memcpy(pcm_store + pcm_used, s_pcm, bytes);
                        pcm_used += bytes;
                    } else {
                        status = ST_PCM_OVERFLOW;
                    }
                }
            }
            if (p == before) /* adapter contract violated: avoid spinning */
                break;
        }
        if (cycles < best)
            best = cycles;
        total += cycles;
    }

    size_t min_free = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
    r.heap_min_free_delta = base_free > min_free ? (uint32_t)(base_free - min_free) : 0;
    r.stack_hwm = (uint32_t)uxTaskGetStackHighWaterMark(NULL);

    codec_close();
    heap_caps_free(clip);

    if (status == ST_OK && frames == 0)
        status = ST_NO_FRAMES;

    r.status = status;
    r.frames = frames;
    r.samples_out = samples_out;
    r.out_rate = (uint32_t)out_rate;
    r.out_channels = (uint32_t)out_channels;
    r.best_cycles = best == UINT64_MAX ? 0 : best;
    r.mean_cycles = total / loops;
    r.pcm_crc32 = ~crc;

    int send_pcm = pcm_store && status == ST_OK;
    send_result(&r, send_pcm ? pcm_store : NULL, send_pcm ? (size_t)samples_out * 2 : 0);
    if (pcm_store)
        heap_caps_free(pcm_store);
}

void app_main(void)
{
    crc_init();
    s_pcm = heap_caps_malloc(CODEC_PCM_MAX_SAMPLES * sizeof(int16_t),
                             MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!s_pcm || transport_init() != 0) {
        ESP_LOGE(TAG, "init failed");
        return;
    }
    ESP_LOGI(TAG, "ready: codec=%s sbr=%d psram=%d internal_free=%u largest_block=%u", CODEC_NAME, CODEC_SBR, have_psram(),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    /* The data port is binary from here on; the host resyncs on "AACR". */
    esp_log_level_set("*", ESP_LOG_NONE);

    for (;;) {
        if (wait_magic("AACB") != 0)
            continue;
        serve_request();
    }
}
