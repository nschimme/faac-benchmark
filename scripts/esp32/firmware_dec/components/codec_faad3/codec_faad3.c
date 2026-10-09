#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "esp_heap_caps.h"
#include "esp_rom_sys.h"
#include "codec_alloc.h"
#include "faad.h"
#include "codec_api.h"

static faad_decoder *s_dec;
static void *s_state;   /* caller-owned decoder state */
static void *s_pcm;     /* FAAD3 writes up to max_output_bytes per call */
static uint32_t s_pcm_cap;

int codec_init(void)
{
    faad_config cfg;
    if (faad_config_init(&cfg, sizeof(cfg)) != FAAD_OK)
        return -1;
    cfg.stream_format = FAAD_STREAM_ADTS;
    cfg.output_format = FAAD_OUTPUT_16BIT;

#ifdef CODEC_FAAD3_OPEN
    /* The library allocates its state as several independent blocks. */
    faad_status st = faad_decoder_open(&cfg, NULL, 0, &s_dec);
    if (st != FAAD_OK) {
        esp_rom_printf("faad3: faad_decoder_open: %s (largest block %u)\n", faad_strerror(st),
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
        codec_close();
        return -1;
    }
#else
    uint32_t state_bytes = 0;
    if (faad_get_state_size(&state_bytes) != FAAD_OK)
        return -1;
    s_state = heap_caps_aligned_alloc(FAAD_STATE_ALIGNMENT, state_bytes, CODEC_ALLOC_CAPS);
    if (!s_state || faad_decoder_init(s_state, state_bytes, &cfg, NULL, 0, &s_dec) != FAAD_OK) {
        codec_close();
        return -1;
    }
#endif
    faad_stream_info info = { .struct_size = sizeof(info) };
    if (faad_decoder_get_info(s_dec, &info) != FAAD_OK) {
        codec_close();
        return -1;
    }
    s_pcm_cap = info.max_output_bytes;
    s_pcm = heap_caps_aligned_alloc(16, s_pcm_cap, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) /* scratch stays internal */;
    if (!s_pcm) {
        esp_rom_printf("faad3: pcm scratch %u B failed (largest block %u)\n", (unsigned)s_pcm_cap,
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
        codec_close();
        return -1;
    }
    return 0;
}

void codec_close(void)
{
#ifdef CODEC_FAAD3_OPEN
    faad_decoder_close(&s_dec);
#endif
    s_dec = NULL; /* single-block state is caller-owned: freed below */
    heap_caps_free(s_state);
    heap_caps_free(s_pcm);
    s_state = s_pcm = NULL;
}

int codec_decode_frame(const uint8_t **in, int *bytes_left, int16_t *pcm,
                       codec_frame_info_t *fi)
{
    if (!s_dec || *bytes_left <= 0)
        return 1;

    uint32_t used = 0, written = 0, flags = 0;
    faad_status st = faad_decode_frame(s_dec, *in, (uint32_t)*bytes_left, &used,
                                       s_pcm, s_pcm_cap, &written, &flags);
    if (used == 0)
        used = (st == FAAD_ERR_NEED_MORE_DATA) ? (uint32_t)*bytes_left : 1;
    if (used > (uint32_t)*bytes_left)
        used = (uint32_t)*bytes_left;
    *in += used;
    *bytes_left -= (int)used;

    if (st == FAAD_ERR_NEED_MORE_DATA || st == FAAD_ERR_SYNC_LOST)
        return 1;
    if (st != FAAD_OK)
        return -1;
    if (written == 0)
        return 1;
    if (written > CODEC_PCM_MAX_SAMPLES * sizeof(int16_t))
        return -1;

    faad_stream_info info = { .struct_size = sizeof(info) };
    if (faad_decoder_get_info(s_dec, &info) != FAAD_OK)
        return -1;
    memcpy(pcm, s_pcm, written);
    fi->out_rate = (int)info.sample_rate;
    fi->out_channels = (int)info.channels;
    fi->out_samples = (int)(written / sizeof(int16_t));
    return 0;
}
