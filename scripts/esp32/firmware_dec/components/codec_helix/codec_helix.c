#include <stddef.h>
#include <stdint.h>
#include "aacdec.h"
#include "codec_api.h"
#include "esp_heap_caps.h"
#include "codec_alloc.h"
#include "utils/helix_memory.h"

// Helix state placement follows CODEC_PSRAM (internal RAM by default).
void *helix_malloc(int size) { return heap_caps_malloc(size, CODEC_ALLOC_CAPS); }
void helix_free(void *ptr) { heap_caps_free(ptr); }

static HAACDecoder s_dec;

int codec_init(void)
{
    s_dec = AACInitDecoder();
    return s_dec ? 0 : -1;
}

void codec_close(void)
{
    if (s_dec) {
        AACFreeDecoder(s_dec);
        s_dec = NULL;
    }
}

int codec_decode_frame(const uint8_t **in, int *bytes_left, int16_t *pcm,
                       codec_frame_info_t *fi)
{
    if (!s_dec || *bytes_left <= 0)
        return 1;

    unsigned char *p = (unsigned char *)*in;
    int n = *bytes_left;

    int off = AACFindSyncWord(p, n);
    if (off < 0) {
        *in += n;
        *bytes_left = 0;
        return 1;
    }
    p += off;
    n -= off;

    unsigned char *dp = p;
    int left = n;
    int err = AACDecode(s_dec, &dp, &left, (short *)pcm);
    int consumed = n - left;

    if (consumed <= 0) {
        /* Helix leaves the pointer untouched on failure: step past the
         * sync word so the next call searches further on. */
        consumed = 1;
    }
    *in = p + consumed;
    *bytes_left = n - consumed;

    if (err == ERR_AAC_INDATA_UNDERFLOW)
        return 1;
    if (err != ERR_AAC_NONE)
        return -1;

    AACFrameInfo info;
    AACGetLastFrameInfo(s_dec, &info);
    if (info.outputSamps <= 0)
        return 1;
    fi->out_rate = info.sampRateOut;
    fi->out_channels = info.nChans;
    fi->out_samples = info.outputSamps;
    return 0;
}
