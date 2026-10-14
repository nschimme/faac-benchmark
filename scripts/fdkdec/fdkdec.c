/* fdkdec: decode an MP4/M4A or raw ADTS AAC stream with libfdk-aac.
 * MP4 samples and gapless metadata come from libfaam. Writes <out>_raw.wav
 * (untrimmed decoder output) and <out>.wav (trimmed by container priming,
 * padding, and decoder outputDelay; ADTS has no container gapless metadata),
 * and prints fdk's reported
 * outputDelay. Also supports `fdkdec -v` / `--version` to print the
 * linked libfdk-aac version for tool auto-detection. */
#define _POSIX_C_SOURCE 200809L
#define _FILE_OFFSET_BITS 64
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <stdint.h>
#include <stdbool.h>
#include <sys/types.h>
#include <faam.h>
#include <fdk-aac/aacdecoder_lib.h>

static int32_t faam_read(void *user_data, void *buf, uint32_t bytes)
{
    size_t n = fread(buf, 1, bytes, (FILE *)user_data);
    return ferror((FILE *)user_data) || n > INT32_MAX ? -1 : (int32_t)n;
}

static bool faam_seek(void *user_data, uint64_t offset)
{
    return offset <= INT64_MAX && fseeko((FILE *)user_data, (off_t)offset, SEEK_SET) == 0;
}

static uint64_t faam_tell(void *user_data)
{
    off_t pos = ftello((FILE *)user_data);
    return pos < 0 ? UINT64_MAX : (uint64_t)pos;
}

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

static bool looks_like_adts(const uint8_t *buf, size_t len) {
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
    if (fseeko(in, 0, SEEK_END) != 0) { fclose(in); return 1; }
    off_t file_len = ftello(in);
    if (file_len <= 0 || file_len > UINT32_MAX || fseeko(in, 0, SEEK_SET) != 0) { fclose(in); return 1; }
    size_t len = (size_t)file_len;
    uint8_t *buf = malloc(len);
    if (!buf || fread(buf, 1, len, in) != (size_t)len) { free(buf); fclose(in); return 1; }

    faam_demuxer *demuxer = NULL;
    faam_track_info track = { .struct_size = sizeof(track) };
    faam_gapless_info gapless = { .struct_size = sizeof(gapless) };
    faam_io io = { .struct_size = sizeof(io), .user_data = in,
                   .read = faam_read, .seek = faam_seek, .tell = faam_tell };
    bool is_mp4 = faam_demuxer_open(NULL, &io, &demuxer) == FAAM_OK;
    if (is_mp4) {
        uint32_t count = 0;
        faam_demuxer_get_num_tracks(demuxer, &count, NULL);
        for (uint32_t i = 0; i < count; i++) {
            faam_track_info candidate = { .struct_size = sizeof(candidate) };
            if (faam_demuxer_get_track_info(demuxer, i, &candidate) == FAAM_OK &&
                candidate.track_type == FAAM_TRACK_AUDIO && candidate.codec_id == FAAM_CODEC_AAC) {
                track = candidate;
                break;
            }
        }
        if (!track.track_id) {
            /* libfaam also opens raw ADTS but exposes no AAC track: fall
             * through to the ADTS path unless this is not ADTS either. */
            faam_demuxer_close(&demuxer);
            demuxer = NULL;
            is_mp4 = false;
            if (!looks_like_adts(buf, len)) {
                fprintf(stderr, "no AAC audio track in MP4\n");
                fclose(in);
                free(buf);
                return 1;
            }
        } else {
            faam_demuxer_get_track_gapless(demuxer, track.track_id, &gapless);
        }
    }
    bool is_adts = !is_mp4 && looks_like_adts(buf, len);
    if (!is_mp4 && !is_adts) { fprintf(stderr, "not mp4 or adts\n"); fclose(in); free(buf); return 1; }

    HANDLE_AACDECODER dec;
    if (is_mp4) {
        dec = aacDecoder_Open(TT_MP4_RAW, 1);
        if (!dec) { fprintf(stderr, "cannot open libfdk-aac decoder\n"); faam_demuxer_close(&demuxer); fclose(in); free(buf); return 1; }
        uint8_t asc_buf[FAAM_CODEC_DATA_MAX];
        uint32_t asc_size = 0;
        if (faam_demuxer_get_codec_data(demuxer, track.track_id, asc_buf,
                                         sizeof(asc_buf), &asc_size) != FAAM_OK) {
            fprintf(stderr, "cannot read AAC decoder configuration\n");
            faam_demuxer_close(&demuxer);
            fclose(in);
            free(buf);
            return 1;
        }
        UCHAR *asc = asc_buf; UINT asc_len = asc_size;
        if (aacDecoder_ConfigRaw(dec, &asc, &asc_len) != AAC_DEC_OK) {
            fprintf(stderr, "ConfigRaw failed\n");
            aacDecoder_Close(dec);
            faam_demuxer_close(&demuxer);
            fclose(in);
            free(buf);
            return 1;
        }
    } else {
        dec = aacDecoder_Open(TT_MP4_ADTS, 1);
        if (!dec) { fprintf(stderr, "cannot open libfdk-aac decoder\n"); fclose(in); free(buf); return 1; }
    }
    aacDecoder_SetParam(dec, AAC_PCM_LIMITER_ENABLE, 0);

    size_t cap = 1 << 20, n = 0;   /* interleaved samples */
    int16_t *pcm = malloc(cap * sizeof(int16_t));
    INT_PCM out[8 * 2048 * 2];
    CStreamInfo *si = NULL;
    uint32_t core_rate = 0;

    if (is_mp4) {
        uint32_t frame_cap = track.max_frame_bytes ? track.max_frame_bytes : 65536;
        UCHAR *frame = malloc(frame_cap);
        if (!frame) { fprintf(stderr, "out of memory\n"); return 1; }
        uint32_t frame_index = 0, frame_bytes = 0;
        faam_frame_loc loc = { .struct_size = sizeof(loc) };
        while (faam_demuxer_next_frame_loc(demuxer, &loc) == FAAM_OK) {
            if (loc.track_id != track.track_id) {
                faam_demuxer_read_frame(demuxer, NULL, 0, &frame_bytes);
                continue;
            }
            faam_status fs = faam_demuxer_read_frame(demuxer, frame, frame_cap, &frame_bytes);
            if (fs == FAAM_ERR_OUTPUT_TOO_SMALL) {
                UCHAR *larger = realloc(frame, frame_bytes);
                if (!larger) { fprintf(stderr, "out of memory\n"); free(frame); return 1; }
                frame = larger;
                frame_cap = frame_bytes;
                fs = faam_demuxer_read_frame(demuxer, frame, frame_cap, &frame_bytes);
            }
            if (fs != FAAM_OK) { fprintf(stderr, "MP4 frame read failed: %s\n", faam_strerror(fs)); break; }
            UCHAR *p = frame; UINT sz = frame_bytes, valid = frame_bytes;
            if (aacDecoder_Fill(dec, &p, &sz, &valid) != AAC_DEC_OK) break;
            AAC_DECODER_ERROR e = aacDecoder_DecodeFrame(dec, out, sizeof(out) / sizeof(out[0]), 0);
            frame_index++;
            if (e == AAC_DEC_NOT_ENOUGH_BITS) continue;
            if (e != AAC_DEC_OK) { fprintf(stderr, "frame %u err 0x%x\n", frame_index, e); continue; }
            si = aacDecoder_GetStreamInfo(dec);
            if (!core_rate) core_rate = si->aacSampleRate;
            size_t k = (size_t)si->frameSize * si->numChannels;
            if (n + k > cap) { cap = (n + k) * 2; pcm = realloc(pcm, cap * sizeof(int16_t)); }
            memcpy(pcm + n, out, k * sizeof(int16_t));
            n += k;
        }
        free(frame);
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
    if (!si) {
        fprintf(stderr, "no output\n");
        faam_demuxer_close(&demuxer);
        aacDecoder_Close(dec);
        fclose(in);
        free(buf);
        free(pcm);
        return 1;
    }

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
     * ADTS input carries no such metadata, so delay and padding are 0
     * and only the decoder's own outputDelay is trimmed. */
    uint64_t skip = (uint64_t)gapless.encoder_delay * scale + si->outputDelay;
    uint64_t media = (uint64_t)frames;
    uint64_t keep = media > skip + (uint64_t)gapless.end_padding * scale ? media - skip - (uint64_t)gapless.end_padding * scale : 0;
    if (skip + keep > media) keep = media - skip;
    f = fopen(out_path, "wb"); wav_hdr(f, sr, ch, (uint32_t)keep); fwrite(pcm + skip * ch, 2, keep * ch, f); fclose(f);

    printf("aot=%d core=%u out=%u ch=%u scale=%u priming=%u padding=%u outputDelay=%u raw_frames=%u gapless_frames=%llu\n",
           si->aot, core_rate, sr, ch, scale, gapless.encoder_delay, gapless.end_padding, si->outputDelay, frames, (unsigned long long)keep);
    faam_demuxer_close(&demuxer);
    aacDecoder_Close(dec);
    fclose(in);
    free(buf);
    return 0;
}
