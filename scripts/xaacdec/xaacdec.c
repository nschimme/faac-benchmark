#define _DEFAULT_SOURCE
/*
 * xaacdec: decode an MP4/M4A or raw ADTS AAC stream with libxaac & libfaam.
 * MP4 samples and gapless metadata come from libfaam.
 * Writes <out>.wav (trimmed by container priming and padding for gapless playback).
 * Uses libfaam directly for MP4 demuxing without spawning any external tools.
 */

#define _POSIX_C_SOURCE 200809L
#define _FILE_OFFSET_BITS 64
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <stdint.h>
#include <stdbool.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <libgen.h>

#include <faam.h>

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

static int32_t faam_read(void *user_data, void *buf, uint32_t bytes) {
    size_t n = fread(buf, 1, bytes, (FILE *)user_data);
    return ferror((FILE *)user_data) || n > INT32_MAX ? -1 : (int32_t)n;
}

static bool faam_seek(void *user_data, uint64_t offset) {
    return offset <= INT64_MAX && fseeko((FILE *)user_data, (off_t)offset, SEEK_SET) == 0;
}

static uint64_t faam_tell(void *user_data) {
    off_t pos = ftello((FILE *)user_data);
    return pos < 0 ? UINT64_MAX : (uint64_t)pos;
}

static int sr_to_idx(uint32_t sr) {
    switch (sr) {
        case 96000: return 0;
        case 88200: return 1;
        case 64000: return 2;
        case 48000: return 3;
        case 44100: return 4;
        case 32000: return 5;
        case 24000: return 6;
        case 22050: return 7;
        case 16000: return 8;
        case 12000: return 9;
        case 11025: return 10;
        case 8000:  return 11;
        case 7350:  return 12;
        default:    return 4;
    }
}

static void make_adts_header(uint8_t *dst, int aot, int sr_idx, int ch, int frame_len) {
    int total_len = frame_len + 7;
    dst[0] = 0xFF;
    dst[1] = 0xF1;
    dst[2] = ((aot - 1) << 6) | (sr_idx << 2) | ((ch >> 2) & 1);
    dst[3] = ((ch & 3) << 6) | ((total_len >> 11) & 3);
    dst[4] = (total_len >> 3) & 0xFF;
    dst[5] = ((total_len & 7) << 5) | 0x1F;
    dst[6] = 0xFC;
}

static void find_sibling_core_bin(const char *self_path, const char *core_name, char *out_bin, size_t out_size) {
    char path_buf[1024];
    strncpy(path_buf, self_path, sizeof(path_buf) - 1);
    char *dir = dirname(path_buf);
    snprintf(out_bin, out_size, "%s/%s", dir, core_name);
    if (access(out_bin, X_OK) == 0) return;
    snprintf(out_bin, out_size, "%s", core_name);
}

int main(int argc, char **argv) {
    if (argc >= 2 && (!strcmp(argv[1], "-v") || !strcmp(argv[1], "--version"))) {
        printf("xaacdec (Ittiam libxaac 1.41)\n");
        return 0;
    }

    char in_path[1024] = {0};
    char out_path[1024] = {0};
    bool write_raw = false;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--raw")) write_raw = true;
        else if (!strncmp(argv[i], "-ifile:", 7)) strncpy(in_path, argv[i] + 7, sizeof(in_path) - 1);
        else if (!strncmp(argv[i], "-ofile:", 7)) strncpy(out_path, argv[i] + 7, sizeof(out_path) - 1);
        else if (argv[i][0] != '-') {
            if (!in_path[0]) strncpy(in_path, argv[i], sizeof(in_path) - 1);
            else if (!out_path[0]) strncpy(out_path, argv[i], sizeof(out_path) - 1);
        }
    }

    if (!in_path[0] || !out_path[0]) {
        fprintf(stderr, "usage: %s in.m4a|in.aac out.wav [--raw]\n", argv[0]);
        return 2;
    }

    FILE *in = fopen(in_path, "rb");
    if (!in) { perror("fopen input"); return 1; }

    faam_demuxer *demuxer = NULL;
    faam_track_info track = { .struct_size = sizeof(track) };
    faam_gapless_info gapless = { .struct_size = sizeof(gapless) };
    faam_io io = { .struct_size = sizeof(io), .user_data = in,
                   .read = faam_read, .seek = faam_seek, .tell = faam_tell };

    bool is_mp4 = faam_demuxer_open(NULL, &io, &demuxer) == FAAM_OK;
    char demux_aac[1024] = {0};

    if (is_mp4) {
        uint32_t count = 0;
        faam_demuxer_get_num_tracks(demuxer, &count, NULL);
        for (uint32_t i = 0; i < count; i++) {
            faam_track_info cand = { .struct_size = sizeof(cand) };
            if (faam_demuxer_get_track_info(demuxer, i, &cand) == FAAM_OK &&
                cand.track_type == FAAM_TRACK_AUDIO && cand.codec_id == FAAM_CODEC_AAC) {
                track = cand;
                break;
            }
        }
        if (track.track_id) {
            faam_demuxer_get_track_gapless(demuxer, track.track_id, &gapless);
        }

        /* Demux AAC access units directly using libfaam into an ADTS stream */
        char template_aac[] = "/tmp/xaac_demux_XXXXXX.aac";
        int fd = mkstemps(template_aac, 4);
        if (fd < 0) {
            faam_demuxer_close(&demuxer);
            fclose(in);
            return 1;
        }
        close(fd);
        strncpy(demux_aac, template_aac, sizeof(demux_aac) - 1);

        FILE *adts_out = fopen(demux_aac, "wb");
        if (!adts_out) {
            faam_demuxer_close(&demuxer);
            fclose(in);
            return 1;
        }

        uint8_t asc[FAAM_CODEC_DATA_MAX];
        uint32_t asc_len = 0;
        int aot = 2;
        int sr_idx = 4;
        int ch = 2;

        if (faam_demuxer_get_codec_data(demuxer, track.track_id, asc, sizeof(asc), &asc_len) == FAAM_OK && asc_len >= 2) {
            aot = (asc[0] >> 3) & 0x1f;
            sr_idx = ((asc[0] & 7) << 1) | ((asc[1] >> 7) & 1);
            ch = (asc[1] >> 3) & 0x0f;
        } else if (track.sample_rate) {
            sr_idx = sr_to_idx(track.sample_rate);
            ch = track.channels ? track.channels : 2;
        }

        uint8_t *frame_buf = malloc(65536);
        uint32_t frame_cap = 65536;
        uint32_t frame_bytes = 0;
        faam_frame_loc loc = { .struct_size = sizeof(loc) };
        uint8_t adts_hdr[7];

        while (faam_demuxer_next_frame_loc(demuxer, &loc) == FAAM_OK) {
            if (loc.track_id != track.track_id) {
                faam_demuxer_read_frame(demuxer, NULL, 0, &frame_bytes);
                continue;
            }
            faam_status fs = faam_demuxer_read_frame(demuxer, frame_buf, frame_cap, &frame_bytes);
            if (fs == FAAM_ERR_OUTPUT_TOO_SMALL) {
                uint8_t *bigger = realloc(frame_buf, frame_bytes);
                if (!bigger) break;
                frame_buf = bigger;
                frame_cap = frame_bytes;
                fs = faam_demuxer_read_frame(demuxer, frame_buf, frame_cap, &frame_bytes);
            }
            if (fs != FAAM_OK) break;
            if (frame_bytes > 0) {
                make_adts_header(adts_hdr, aot, sr_idx, ch, (int)frame_bytes);
                fwrite(adts_hdr, 1, 7, adts_out);
                fwrite(frame_buf, 1, frame_bytes, adts_out);
            }
        }

        free(frame_buf);
        fclose(adts_out);
        faam_demuxer_close(&demuxer);
        fclose(in);
    } else {
        fclose(in);
        strncpy(demux_aac, in_path, sizeof(demux_aac) - 1);
    }

    /* Decode using libxaac core decoder safely without shell injection */
    char raw_wav[] = "/tmp/xaac_raw_XXXXXX.wav";
    int rfd = mkstemps(raw_wav, 4);
    if (rfd >= 0) close(rfd);

    char dec_bin[1024];
    find_sibling_core_bin(argv[0], "xaacdec_core", dec_bin, sizeof(dec_bin));

    char ifile_arg[1100], ofile_arg[1100];
    snprintf(ifile_arg, sizeof(ifile_arg), "-ifile:%s", demux_aac);
    snprintf(ofile_arg, sizeof(ofile_arg), "-ofile:%s", raw_wav);

    pid_t child = fork();
    int status = 0;
    if (child == 0) {
        // Suppress stdout/stderr
        FILE *null = fopen("/dev/null", "wb");
        if (null) {
            dup2(fileno(null), STDOUT_FILENO);
            dup2(fileno(null), STDERR_FILENO);
            fclose(null);
        }
        char *exec_args[] = {dec_bin, ifile_arg, ofile_arg, NULL};
        execvp(dec_bin, exec_args);
        _exit(127);
    } else if (child > 0) {
        waitpid(child, &status, 0);
    } else {
        status = -1;
    }

    if (is_mp4 && demux_aac[0] && strcmp(demux_aac, in_path) != 0) {
        unlink(demux_aac);
    }

    if (status != 0 || access(raw_wav, F_OK) != 0) {
        unlink(raw_wav);
        return 1;
    }

    /* Read decoded raw PCM */
    FILE *raw_f = fopen(raw_wav, "rb");
    if (!raw_f) { unlink(raw_wav); return 1; }

    uint8_t hdr[44];
    if (fread(hdr, 1, 44, raw_f) != 44) { fclose(raw_f); unlink(raw_wav); return 1; }

    uint32_t sr = *(uint32_t *)(hdr + 24);
    uint16_t ch = *(uint16_t *)(hdr + 22);
    fseeko(raw_f, 0, SEEK_END);
    off_t total_file_size = ftello(raw_f);
    uint32_t pcm_bytes = (total_file_size > 44) ? (uint32_t)(total_file_size - 44) : 0;
    uint32_t total_samples = (ch > 0) ? (pcm_bytes / (ch * 2)) : 0;

    int16_t *pcm = malloc(pcm_bytes ? pcm_bytes : 2);
    fseeko(raw_f, 44, SEEK_SET);
    if (pcm && pcm_bytes) {
        (void)fread(pcm, 1, pcm_bytes, raw_f);
    }
    fclose(raw_f);
    unlink(raw_wav);

    if (!pcm) return 1;

    if (write_raw) {
        char raw_out[1024];
        snprintf(raw_out, sizeof(raw_out), "%.*s_raw.wav", (int)(strlen(out_path) - 4), out_path);
        FILE *rf = fopen(raw_out, "wb");
        if (rf) {
            wav_hdr(rf, sr, ch, total_samples);
            fwrite(pcm, 2, total_samples * ch, rf);
            fclose(rf);
        }
    }

    /* Apply gapless trim */
    uint32_t scale = 1;
    if (track.sample_rate && sr > track.sample_rate) scale = sr / track.sample_rate;

    uint64_t skip = (uint64_t)gapless.encoder_delay * scale;
    uint64_t media = total_samples;
    uint64_t keep = (media > skip + (uint64_t)gapless.end_padding * scale) ? (media - skip - (uint64_t)gapless.end_padding * scale) : (media > skip ? media - skip : media);
    if (skip + keep > media) keep = media - skip;

    FILE *out = fopen(out_path, "wb");
    if (!out) { free(pcm); return 1; }
    wav_hdr(out, sr, ch, (uint32_t)keep);
    fwrite(pcm + skip * ch, 2, keep * ch, out);
    fclose(out);
    free(pcm);

    return 0;
}
