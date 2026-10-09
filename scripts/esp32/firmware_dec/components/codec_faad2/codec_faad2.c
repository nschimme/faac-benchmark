#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "neaacdec.h"
#include "codec_api.h"
#include "codec_alloc.h"
#include "esp_heap_caps.h"

void *faad_malloc(size_t size) { return heap_caps_malloc(size, CODEC_ALLOC_CAPS); }
void faad_free(void *b) { heap_caps_free(b); }

static NeAACDecHandle s_dec;
static int s_inited; /* NeAACDecInit needs the first ADTS frame */

int codec_init(void)
{
    s_inited = 0;
    s_dec = NeAACDecOpen();
    if (!s_dec)
        return -1;
    NeAACDecConfigurationPtr c = NeAACDecGetCurrentConfiguration(s_dec);
    c->outputFormat = FAAD_FMT_16BIT;
    c->downMatrix = 0;
    c->dontUpSampleImplicitSBR = 1; // plain LC at <=24 kHz must not be run through implicit SBR
    c->defObjectType = LC;
    if (!NeAACDecSetConfiguration(s_dec, c)) {
        NeAACDecClose(s_dec);
        s_dec = NULL;
        return -1;
    }
    return 0;
}

void codec_close(void)
{
    if (s_dec) {
        NeAACDecClose(s_dec);
        s_dec = NULL;
    }
    s_inited = 0;
}

static int find_adts_sync(const uint8_t *p, int n)
{
    for (int i = 0; i + 7 <= n; i++) {
        if (p[i] == 0xFF && (p[i + 1] & 0xF6) == 0xF0)
            return i;
    }
    return -1;
}

int codec_decode_frame(const uint8_t **in, int *bytes_left, int16_t *pcm,
                       codec_frame_info_t *fi)
{
    if (!s_dec || *bytes_left <= 0)
        return 1;

    const uint8_t *p = *in;
    int n = *bytes_left;

    int off = find_adts_sync(p, n);
    if (off < 0) {
        *in += n;
        *bytes_left = 0;
        return 1;
    }
    p += off;
    n -= off;

    if (!s_inited) {
        unsigned long sr = 0;
        unsigned char ch = 0;
        long skip = NeAACDecInit(s_dec, (unsigned char *)p, (unsigned long)n, &sr, &ch);
        if (skip < 0) {
            *in = p + 1;
            *bytes_left = n - 1;
            return -1;
        }
        s_inited = 1;
        p += skip;
        n -= (int)skip;
    }

    NeAACDecFrameInfo info;
    memset(&info, 0, sizeof(info));
    void *out = NeAACDecDecode(s_dec, &info, (unsigned char *)p, (unsigned long)n);

    int consumed = (int)info.bytesconsumed;
    if (consumed <= 0 || consumed > n)
        consumed = 1;
    *in = p + consumed;
    *bytes_left = n - consumed;

    if (info.error != 0)
        return -1;
    if (!out || info.samples == 0)
        return 1;
    if (info.samples > CODEC_PCM_MAX_SAMPLES)
        return -1;

    memcpy(pcm, out, info.samples * sizeof(int16_t));
    fi->out_rate = (int)info.samplerate;
    fi->out_channels = info.channels;
    fi->out_samples = (int)info.samples;
    return 0;
}
