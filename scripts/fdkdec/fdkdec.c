/* fdkdec: decode an MP4/M4A or raw ADTS AAC stream with libfdk-aac.
 * Writes <out>_raw.wav (untrimmed decoder output) and <out>_gapless.wav
 * (start trim = priming*rate_scale + outputDelay, length = source length
 * derived from iTunSMPB/elst for MP4; ADTS input has no container gapless
 * metadata, so only outputDelay is trimmed), and prints fdk's reported
 * outputDelay. Also supports `fdkdec -v` / `--version` to print the
 * linked libfdk-aac version for tool auto-detection. */
#define HAVE_LIBFAAM 1
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include "mp4read.c"
#include <fdk-aac/aacdecoder_lib.h>

static void wav_hdr(FILE *f, uint32_t sr, uint32_t ch, uint32_t frames) {
    uint32_t data = frames * ch * 2, v;
    uint16_t s;
    fwrite("RIFF", 1, 4, f); v = 36 + data; fwrite(&v, 4, 1, f);
    fwrite("WAVEfmt ", 1, 8, f); v = 16; fwrite(&v, 4, 1, f);
    s = 1; fwrite(&s, 2, 1, f); s = (uint16_t)ch; fwrite(&s, 2, 1, f);
    fwrite(&sr, 4, 1, f); v = sr * ch * 2; fwrite(&v, 4, 1, f);
    s = (uint16_t)(ch * 2); fwrite(&s, 2, 1, f); s = 16; fwrite(&s, 2, 1, f);
    fwrite("data", 1, 4, f); fwrite(&data, 4, 1, f);
}

static bool looks_like_adts(const uint8_t *buf, long len) {
    return len >= 2 && buf[0] == 0xFF && (buf[1] & 0xF6) == 0xF0;
}

int main(int argc, char **argv) {
    if (argc >= 2 && (!strcmp(argv[1], "-v") || !strcmp(argv[1], "--version"))) {
        LIB_INFO info[FDK_MODULE_LAST];
        memset(info, 0, sizeof(info));
        HANDLE_AACDECODER d = aacDecoder_Open(TT_MP4_ADTS, 1);
        aacDecoder_GetLibInfo(info);
        for (int i = 0; i < FDK_MODULE_LAST; i++) {
            if (info[i].module_id == FDK_AACDEC)
                printf("fdkdec (libfdk-aac %d.%d.%d)\n",
                       (info[i].version >> 16) & 0xFF, (info[i].version >> 8) & 0xFF, info[i].version & 0xFF);
        }
        aacDecoder_Close(d);
        return 0;
    }
    if (argc < 3) { fprintf(stderr, "usage: fdkdec in.m4a|in.aac out.wav\n"); return 2; }
    FILE *in = fopen(argv[1], "rb");
    if (!in) return 1;
    fseek(in, 0, SEEK_END); long len = ftell(in); fseek(in, 0, SEEK_SET);
    uint8_t *buf = malloc(len);
    if (fread(buf, 1, len, in) != (size_t)len) return 1;
    fclose(in);

    MP4Track tr;
    memset(&tr, 0, sizeof(tr));
    bool is_mp4 = mp4_read_track_buf(buf, len, &tr);
    bool is_adts = !is_mp4 && looks_like_adts(buf, len);
    if (!is_mp4 && !is_adts) { fprintf(stderr, "not mp4 or adts\n"); return 1; }

    HANDLE_AACDECODER dec;
    if (is_mp4) {
        dec = aacDecoder_Open(TT_MP4_RAW, 1);
        UCHAR *asc = tr.asc_buf; UINT asc_len = tr.asc_len;
        if (aacDecoder_ConfigRaw(dec, &asc, &asc_len) != AAC_DEC_OK) { fprintf(stderr, "ConfigRaw failed\n"); return 1; }
    } else {
        dec = aacDecoder_Open(TT_MP4_ADTS, 1);
    }
    aacDecoder_SetParam(dec, AAC_PCM_LIMITER_ENABLE, 0);

    size_t cap = 1 << 20, n = 0;   /* interleaved samples */
    int16_t *pcm = malloc(cap * sizeof(int16_t));
    INT_PCM out[8 * 2048 * 2];
    CStreamInfo *si = NULL;
    uint32_t core_rate = 0;

    if (is_mp4) {
        for (uint32_t i = 0; i < tr.num_samples; i++) {
            UCHAR *p = buf + tr.samples[i].offset; UINT sz = tr.samples[i].size, valid = sz;
            if (aacDecoder_Fill(dec, &p, &sz, &valid) != AAC_DEC_OK) break;
            AAC_DECODER_ERROR e = aacDecoder_DecodeFrame(dec, out, sizeof(out) / sizeof(out[0]), 0);
            if (e == AAC_DEC_NOT_ENOUGH_BITS) continue;
            if (e != AAC_DEC_OK) { fprintf(stderr, "frame %u err 0x%x\n", i, e); continue; }
            si = aacDecoder_GetStreamInfo(dec);
            if (!core_rate) core_rate = si->aacSampleRate;
            size_t k = (size_t)si->frameSize * si->numChannels;
            if (n + k > cap) { cap = (n + k) * 2; pcm = realloc(pcm, cap * sizeof(int16_t)); }
            memcpy(pcm + n, out, k * sizeof(int16_t));
            n += k;
        }
    } else {
        /* Raw ADTS: libfdk finds its own sync words inside the buffer we
         * hand it, so we stream the whole file through Fill/DecodeFrame
         * rather than pre-splitting into access units like the MP4 path. */
        UCHAR *p = buf; UINT size = (UINT)len, valid = size;
        int stall_guard = 0;
        while (size > 0) {
            if (aacDecoder_Fill(dec, &p, &size, &valid) != AAC_DEC_OK) break;
            UINT consumed = size - valid;
            p += consumed; size = valid;
            /* A single Fill can hand the decoder's internal ring buffer far
             * more than one frame (its capacity is tens of AAC frames), so
             * drain every complete frame it now holds before asking for
             * more external input -- one DecodeFrame call per Fill would
             * leave most of the file buffered but never decoded. */
            for (;;) {
                AAC_DECODER_ERROR e = aacDecoder_DecodeFrame(dec, out, sizeof(out) / sizeof(out[0]), 0);
                if (e != AAC_DEC_OK) {
                    if (e != AAC_DEC_NOT_ENOUGH_BITS) fprintf(stderr, "adts decode err 0x%x\n", e);
                    break;
                }
                si = aacDecoder_GetStreamInfo(dec);
                if (!core_rate) core_rate = si->aacSampleRate;
                size_t k = (size_t)si->frameSize * si->numChannels;
                if (n + k > cap) { cap = (n + k) * 2; pcm = realloc(pcm, cap * sizeof(int16_t)); }
                memcpy(pcm + n, out, k * sizeof(int16_t));
                n += k;
            }
            if (consumed == 0) {
                if (++stall_guard > 4) break; /* no progress; bail rather than spin */
            } else {
                stall_guard = 0;
            }
        }
    }
    if (!si) { fprintf(stderr, "no output\n"); return 1; }

    /* Drain the decoder's internal delay so the tail is complete. */
    for (UINT drained = 0; drained < si->outputDelay; ) {
        if (aacDecoder_DecodeFrame(dec, out, sizeof(out) / sizeof(out[0]), AACDEC_FLUSH) != AAC_DEC_OK) break;
        CStreamInfo *fi = aacDecoder_GetStreamInfo(dec);
        size_t k = (size_t)fi->frameSize * fi->numChannels;
        if (!k) break;
        if (n + k > cap) { cap = (n + k) * 2; pcm = realloc(pcm, cap * sizeof(int16_t)); }
        memcpy(pcm + n, out, k * sizeof(int16_t));
        n += k; drained += fi->frameSize;
    }

    uint32_t ch = si->numChannels, sr = si->sampleRate, frames = (uint32_t)(n / ch);
    uint32_t scale = core_rate ? sr / core_rate : 1;
    char path[1024];
    const char *out_path = argv[2];
    size_t out_len = strlen(out_path);
    bool has_wav_ext = out_len > 4 && !strcasecmp(out_path + out_len - 4, ".wav");

    /* argv[2] is the gapless output's exact path; the untrimmed debug wav
     * is written alongside it with a "_raw" suffix inserted before the
     * extension (or appended, if argv[2] has no .wav extension). */
    if (has_wav_ext)
        snprintf(path, sizeof(path), "%.*s_raw.wav", (int)(out_len - 4), out_path);
    else
        snprintf(path, sizeof(path), "%s_raw.wav", out_path);
    FILE *f = fopen(path, "wb"); wav_hdr(f, sr, ch, frames); fwrite(pcm, 2, n, f); fclose(f);

    /* Container gapless counts are in the track timescale (the core rate).
     * ADTS input carries no such metadata, so tr.delay/tr.padding are 0
     * and only the decoder's own outputDelay is trimmed. */
    uint64_t skip = (uint64_t)tr.delay * scale + si->outputDelay;
    uint64_t media = (uint64_t)frames;
    uint64_t keep = media > skip + (uint64_t)tr.padding * scale ? media - skip - (uint64_t)tr.padding * scale : 0;
    if (skip + keep > media) keep = media - skip;
    f = fopen(out_path, "wb"); wav_hdr(f, sr, ch, (uint32_t)keep); fwrite(pcm + skip * ch, 2, keep * ch, f); fclose(f);

    printf("aot=%d core=%u out=%u ch=%u scale=%u priming=%u padding=%u outputDelay=%u raw_frames=%u gapless_frames=%llu\n",
           si->aot, core_rate, sr, ch, scale, tr.delay, tr.padding, si->outputDelay, frames, (unsigned long long)keep);
    aacDecoder_Close(dec);
    return 0;
}
