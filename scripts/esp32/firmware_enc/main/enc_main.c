/* On-device FAAC encoder benchmark. Protocol is described in ../README.md. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_cpu.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "faac.h"
#include "transport.h"

#define TAG "encbench"

enum {
    ST_OK = 0,
    ST_BAD_REQUEST = 1,
    ST_CLIP_TOO_BIG = 2,
    ST_ENC_OPEN = 3,
    ST_BITSTREAM_OVERFLOW = 4,
    ST_NO_FRAMES = 5,
    ST_NO_MEMORY = 6,
    ST_ENC_FAIL = 7,
};

#define FLAG_RETURN_STREAM 1u
#define RESERVE_SLACK (16 * 1024) /* internal heap kept free beyond the encoder's own state */
#define IN_BUF_BYTES (2048 * 2 * 2)  /* HE frame (2048) x 2 channels x int16 */
#define OUT_BUF_BYTES 16384

typedef struct __attribute__((packed)) {
    char magic[4];
    uint32_t status;
    uint32_t frames;
    uint32_t out_bytes;
    uint32_t object_type;    /* resolved */
    uint32_t frame_samples;  /* per channel, resolved */
    uint32_t enc_rate;       /* resolved nominal output rate */
    uint32_t bit_rate;       /* resolved, per channel */
    uint32_t bandwidth;
    uint64_t best_cycles;
    uint64_t mean_cycles;
    uint32_t stream_crc32;
    uint32_t heap_min_free_delta;
    uint32_t stack_hwm;
    uint32_t init_heap_used;
    uint32_t open_cycles;
} result_t;

_Static_assert(sizeof(result_t) == 72, "result_t must be packed to 72 bytes");

typedef struct __attribute__((packed)) {
    uint32_t sample_rate;
    uint32_t channels;
    uint32_t bit_rate; /* per channel, as faac_params.bit_rate */
    uint32_t object_type;
    uint32_t loops;
    uint32_t flags;
    uint32_t pcm_bytes;
} request_t;

static uint8_t *s_in;  /* one input frame, internal RAM so PSRAM latency stays out of the timing */
static uint8_t *s_out; /* one encoded frame */
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

static int have_psram(void)
{
    return heap_caps_get_total_size(MALLOC_CAP_SPIRAM) > 0;
}

/* PSRAM when present, otherwise internal RAM keeping `reserve` free for the encoder. */
static uint8_t *big_alloc(size_t n, size_t reserve)
{
    if (have_psram())
        return heap_caps_malloc(n, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    /* The encoder state is many small allocations, so it only needs the room in total, not in one block. */
    size_t largest = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    size_t total = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (n <= largest && total > reserve && n <= total - reserve)
        return heap_caps_malloc(n, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    return NULL;
}

static void send_result(result_t *r, const uint8_t *stream, size_t stream_bytes)
{
    memcpy(r->magic, "ENCR", 4);
    transport_write(r, sizeof(*r));
    if (stream && stream_bytes)
        transport_write(stream, stream_bytes);
}

static faac_status open_encoder(const request_t *q, faac_encoder **enc)
{
    faac_params p;
    faac_params_init(&p, sizeof(p));
    p.sample_rate = q->sample_rate;
    p.num_channels = q->channels;
    p.bit_rate = q->bit_rate;
    p.object_type = (enum faac_object_type)q->object_type;
    p.output_format = FAAC_STREAM_ADTS;
    p.input_format = FAAC_INPUT_16BIT;
    return faac_encoder_open(&p, enc);
}

static void serve_request(void)
{
    request_t q;
    result_t r;
    memset(&r, 0, sizeof(r));

    if (transport_read(&q, sizeof(q)) != 0)
        return;

    if (q.pcm_bytes == 0 || q.loops == 0 || q.channels == 0 || q.channels > 2 ||
        q.sample_rate == 0 || q.pcm_bytes % (q.channels * 2) != 0) {
        r.status = ST_BAD_REQUEST;
        send_result(&r, NULL, 0);
        return;
    }

    /* Open once up front to learn the state size, so internal-RAM chips can size the PCM around it
     * and a configuration the library rejects fails before any PCM is sent. */
    size_t free0 = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    faac_encoder *probe = NULL;
    if (open_encoder(&q, &probe) != FAAC_OK) {
        r.status = ST_ENC_OPEN;
        send_result(&r, NULL, 0);
        return;
    }
    size_t free1 = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    faac_encoder_close(&probe);
    size_t reserve = (free0 > free1 ? free0 - free1 : 0) + RESERVE_SLACK;

    uint8_t *pcm = big_alloc(q.pcm_bytes, reserve);
    if (!pcm) {
        /* Say why: the largest free block and the room the encoder needs. */
        r.heap_min_free_delta = (uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        r.init_heap_used = (uint32_t)reserve;
        r.status = ST_CLIP_TOO_BIG;
        send_result(&r, NULL, 0);
        return;
    }
    /* Bitstream return buffer: ADTS output is close to the target rate, so twice that is ample. */
    uint8_t *stream = NULL;
    size_t stream_cap = 0;
    if (q.flags & FLAG_RETURN_STREAM) {
        uint64_t secs_x1000 = (uint64_t)q.pcm_bytes * 1000 / ((uint64_t)q.sample_rate * q.channels * 2);
        stream_cap = (size_t)((uint64_t)q.bit_rate * q.channels * secs_x1000 / 8000 * 2 + 8192);
        stream = big_alloc(stream_cap, reserve);
        if (!stream) {
            heap_caps_free(pcm);
            r.heap_min_free_delta = (uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
            r.init_heap_used = (uint32_t)reserve;
            r.status = ST_NO_MEMORY;
            send_result(&r, NULL, 0);
            return;
        }
    }

    /* The probe above outlasts the UART receive buffer, so the host holds the PCM back until this. */
    transport_write("ENCA", 4);
    if (transport_read(pcm, q.pcm_bytes) != 0) {
        heap_caps_free(pcm);
        return;
    }

    size_t base_free = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);

    uint32_t status = ST_OK;
    uint64_t best = UINT64_MAX, total = 0;
    uint32_t crc = 0xFFFFFFFFu;
    uint32_t frames = 0, out_bytes = 0;
    faac_encoder_info info = {.struct_size = sizeof(info)};

    for (uint32_t pass = 0; pass < q.loops && status == ST_OK; pass++) {
        faac_encoder *enc = NULL;
        uint32_t t0 = esp_cpu_get_cycle_count();
        faac_status fs = open_encoder(&q, &enc);
        uint32_t t1 = esp_cpu_get_cycle_count();
        if (fs != FAAC_OK) {
            status = ST_ENC_OPEN;
            break;
        }
        if (pass == 0) {
            size_t after = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
            r.init_heap_used = base_free > after ? (uint32_t)(base_free - after) : 0;
            r.open_cycles = t1 - t0;
            faac_encoder_get_info(enc, &info);
            if (info.max_output_bytes > OUT_BUF_BYTES ||
                info.frame_samples * q.channels * 2 > IN_BUF_BYTES) {
                faac_encoder_close(&enc);
                status = ST_ENC_FAIL;
                break;
            }
        }

        size_t frame_bytes = (size_t)info.frame_samples * q.channels * 2;
        uint64_t cycles = 0;
        size_t off = 0;
        int flushing = 0;

        while (status == ST_OK) {
            uint32_t n_samples = 0;
            if (!flushing) {
                size_t n = q.pcm_bytes - off < frame_bytes ? q.pcm_bytes - off : frame_bytes;
                if (n == 0) {
                    flushing = 1;
                } else {
                    memcpy(s_in, pcm + off, n);
                    off += n;
                    n_samples = (uint32_t)(n / 2);
                }
            }
            uint32_t bw = 0;
            t0 = esp_cpu_get_cycle_count();
            fs = faac_encoder_encode(enc, s_in, n_samples, s_out, OUT_BUF_BYTES, &bw);
            t1 = esp_cpu_get_cycle_count();
            cycles += (uint32_t)(t1 - t0); /* per-call delta survives counter wrap */
            if (fs != FAAC_OK) {
                status = ST_ENC_FAIL;
                break;
            }
            if (pass == 0 && bw) {
                crc = crc_update(crc, s_out, bw);
                frames++;
                if (stream) {
                    if (out_bytes + bw <= stream_cap)
                        memcpy(stream + out_bytes, s_out, bw);
                    else
                        status = ST_BITSTREAM_OVERFLOW;
                }
                out_bytes += bw;
            }
            if (flushing && bw == 0)
                break;
        }
        faac_encoder_close(&enc);
        if (cycles < best)
            best = cycles;
        total += cycles;
    }

    size_t min_free = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
    r.heap_min_free_delta = base_free > min_free ? (uint32_t)(base_free - min_free) : 0;
    r.stack_hwm = (uint32_t)uxTaskGetStackHighWaterMark(NULL);
    heap_caps_free(pcm);

    if (status == ST_OK && frames == 0)
        status = ST_NO_FRAMES;

    r.status = status;
    r.frames = frames;
    r.out_bytes = out_bytes;
    r.object_type = (uint32_t)info.object_type;
    r.frame_samples = info.frame_samples;
    r.enc_rate = info.sample_rate;
    r.bit_rate = info.bit_rate;
    r.bandwidth = info.bandwidth;
    r.best_cycles = best == UINT64_MAX ? 0 : best;
    r.mean_cycles = q.loops ? total / q.loops : 0;
    r.stream_crc32 = ~crc;

    int send_stream = stream && status == ST_OK;
    send_result(&r, send_stream ? stream : NULL, send_stream ? out_bytes : 0);
    if (stream)
        heap_caps_free(stream);
}

void app_main(void)
{
    crc_init();
    s_in = heap_caps_malloc(IN_BUF_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    s_out = heap_caps_malloc(OUT_BUF_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!s_in || !s_out || transport_init() != 0) {
        ESP_LOGE(TAG, "init failed");
        return;
    }
    ESP_LOGI(TAG, "ready: psram=%d", have_psram());
    /* The data port is binary from here on; the host resyncs on "ENCR". */
    esp_log_level_set("*", ESP_LOG_NONE);

    for (;;) {
        if (wait_magic("ENCB") != 0)
            continue;
        serve_request();
    }
}
