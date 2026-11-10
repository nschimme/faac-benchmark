/*
 * xaacdec - Ittiam libxaac M4A Decoder Wrapper using libfaam
 *
 * Uses libfaam to demux MP4/M4A containers into AAC frames, extracts
 * gapless info (priming delay and padding), runs libxaac decoder in-process,
 * and trims priming delay and padding for sample-accurate gapless WAV output.
 * No child processes, no fork/exec.
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <unistd.h>
#include <libgen.h>

#include "ixheaacd_fileifc.h"
#include "ixheaac_type_def.h"
#include "ixheaac_error_standards.h"
#include "ixheaacd_error_handler.h"
#include "ixheaacd_apicmd_standards.h"
#include "ixheaacd_memory_standards.h"
#include "ixheaacd_aac_config.h"
#include "ixheaacd_metadata_read.h"
#include "impd_drc_config_params.h"
#include "faam.h"

/* libxaac decoder testbench interface */
int ixheaacd_main_process(WORD32 argc, pWORD8 argv[]);
void print_usage(void);
VOID ia_testbench_error_handler_init(void);
VOID metadata_mp4_stsz_size_free(metadata_info *meta_info);
extern pVOID g_pv_arr_alloc_memory[];
extern WORD32 g_w_malloc_count;
extern FileWrapperPtr g_pf_inp;
extern FILE *g_pf_out;
extern FILE *g_pf_meta;
extern WORD32 raw_testing;
extern WORD32 eld_testing;
extern WORD32 mpeg_d_drc_on;
extern metadata_info meta_info;

/* Standard stdio faam_io callbacks */
static int32_t file_read(void *ud, void *buf, uint32_t bytes) {
    size_t r = fread(buf, 1, bytes, (FILE *)ud);
    if (r == 0 && ferror((FILE *)ud)) return -1;
    return (int32_t)r;
}

static int32_t file_write(void *ud, const void *buf, uint32_t bytes) {
    size_t w = fwrite(buf, 1, bytes, (FILE *)ud);
    if (w < bytes) return -1;
    return (int32_t)w;
}

static bool file_seek(void *ud, uint64_t offset) {
    return fseeko((FILE *)ud, (off_t)offset, SEEK_SET) == 0;
}

static uint64_t file_tell(void *ud) {
    off_t o = ftello((FILE *)ud);
    return o < 0 ? 0 : (uint64_t)o;
}

static bool file_flush(void *ud) {
    return fflush((FILE *)ud) == 0;
}

static faam_io make_file_io(FILE *f) {
    faam_io io;
    faam_io_init(&io, sizeof(io));
    io.user_data = f;
    io.read = file_read;
    io.write = file_write;
    io.seek = file_seek;
    io.tell = file_tell;
    io.flush = file_flush;
    return io;
}

static void trim_wav(const char *raw_wav, const char *final_wav, uint32_t delay_samples, uint32_t padding_samples) {
    FILE *in = fopen(raw_wav, "rb");
    if (!in) return;

    uint8_t hdr[44];
    if (fread(hdr, 1, 44, in) < 44 || memcmp(hdr, "RIFF", 4) != 0) {
        fclose(in);
        rename(raw_wav, final_wav);
        return;
    }

    uint16_t channels = hdr[22] | (hdr[23] << 8);
    uint16_t bits_per_sample = hdr[34] | (hdr[35] << 8);
    uint32_t orig_bytes = hdr[40] | (hdr[41] << 8) | (hdr[42] << 16) | (hdr[43] << 24);
    uint32_t bytes_per_sample = (bits_per_sample / 8) * channels;

    if (bytes_per_sample == 0) {
        fclose(in);
        rename(raw_wav, final_wav);
        return;
    }

    uint32_t total_samples = orig_bytes / bytes_per_sample;
    if (delay_samples + padding_samples >= total_samples) {
        fclose(in);
        rename(raw_wav, final_wav);
        return;
    }

    uint32_t keep_samples = total_samples - delay_samples - padding_samples;
    uint32_t keep_bytes = keep_samples * bytes_per_sample;

    FILE *out = fopen(final_wav, "wb");
    if (!out) {
        fclose(in);
        rename(raw_wav, final_wav);
        return;
    }

    /* Update WAV header */
    uint32_t riff_size = 36 + keep_bytes;
    hdr[4] = (uint8_t)(riff_size & 0xFF);
    hdr[5] = (uint8_t)((riff_size >> 8) & 0xFF);
    hdr[6] = (uint8_t)((riff_size >> 16) & 0xFF);
    hdr[7] = (uint8_t)((riff_size >> 24) & 0xFF);

    hdr[40] = (uint8_t)(keep_bytes & 0xFF);
    hdr[41] = (uint8_t)((keep_bytes >> 8) & 0xFF);
    hdr[42] = (uint8_t)((keep_bytes >> 16) & 0xFF);
    hdr[43] = (uint8_t)((keep_bytes >> 24) & 0xFF);

    fwrite(hdr, 1, 44, out);

    /* Skip delay */
    fseeko(in, 44 + (off_t)delay_samples * bytes_per_sample, SEEK_SET);

    /* Copy kept PCM bytes */
    uint8_t buf[8192];
    uint32_t remaining = keep_bytes;
    while (remaining > 0) {
        uint32_t chunk = remaining > sizeof(buf) ? (uint32_t)sizeof(buf) : remaining;
        size_t r = fread(buf, 1, chunk, in);
        if (r == 0) break;
        fwrite(buf, 1, r, out);
        remaining -= (uint32_t)r;
    }

    fclose(in);
    fclose(out);
    unlink(raw_wav);
}

int main(int argc, char **argv) {
    const char *in_file = NULL;
    const char *out_wav = NULL;

    for (int i = 1; i < argc; i++) {
        if (!strncmp(argv[i], "-ifile:", 7)) {
            in_file = argv[i] + 7;
        } else if (!strncmp(argv[i], "-ofile:", 7)) {
            out_wav = argv[i] + 7;
        } else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "-help") || !strcmp(argv[i], "-v") || !strcmp(argv[i], "--version")) {
            print_usage();
            return 0;
        }
    }

    if (!in_file || !out_wav) {
        fprintf(stderr, "Usage: %s -ifile:<in.m4a|in.aac> -ofile:<out.wav> [libxaac args]\n", argv[0]);
        return 1;
    }

    /* Test if input file is an MP4/M4A container */
    FILE *fin = fopen(in_file, "rb");
    if (!fin) {
        perror("Failed to open input file");
        return 1;
    }

    uint8_t probe_hdr[16];
    size_t probe_len = fread(probe_hdr, 1, 16, fin);
    fseek(fin, 0, SEEK_SET);

    bool is_mp4 = false;
    if (probe_len >= 8 && memcmp(probe_hdr + 4, "ftyp", 4) == 0) {
        is_mp4 = true;
    }

    faam_gapless_info gapless;
    memset(&gapless, 0, sizeof(gapless));
    gapless.struct_size = sizeof(gapless);

    char tmp_adts[1024];
    const char *decode_input = in_file;

    if (is_mp4) {
        /* Input is MP4/M4A: demux using libfaam directly */
        faam_io io = make_file_io(fin);
        faam_demuxer *demuxer = NULL;
        faam_status st = faam_demuxer_open(NULL, &io, &demuxer);
        if (st != FAAM_OK) {
            fprintf(stderr, "libfaam faam_demuxer_open failed: %s\n", faam_strerror(st));
            fclose(fin);
            return 1;
        }

        /* Extract gapless parameters */
        faam_demuxer_get_track_gapless(demuxer, 0, &gapless);

        uint32_t held_tracks = 0;
        faam_demuxer_get_num_tracks(demuxer, &held_tracks, NULL);
        if (held_tracks == 0) {
            fprintf(stderr, "No tracks found in M4A container\n");
            faam_demuxer_close(&demuxer);
            fclose(fin);
            return 1;
        }

        faam_track_info tinfo;
        memset(&tinfo, 0, sizeof(tinfo));
        tinfo.struct_size = sizeof(tinfo);
        faam_demuxer_get_track_info(demuxer, 0, &tinfo);

        /* Dump frames to temporary ADTS stream */
        snprintf(tmp_adts, sizeof(tmp_adts), "%s.tmp.%d.aac", in_file, (int)getpid());

        FILE *fadts = fopen(tmp_adts, "wb");
        if (!fadts) {
            perror("Failed to create temporary demuxed AAC file");
            faam_demuxer_close(&demuxer);
            fclose(fin);
            return 1;
        }

        uint8_t frame_buf[8192];
        uint32_t frame_len = 0;
        static const uint32_t srs[] = {
            96000, 88200, 64000, 48000, 44100, 32000,
            24000, 22050, 16000, 12000, 11025, 8000, 7350
        };
        int sr_idx = 4;
        for (int i = 0; i < 13; i++) {
            if (srs[i] == tinfo.sample_rate) { sr_idx = i; break; }
        }

        while (faam_demuxer_read_frame(demuxer, frame_buf, sizeof(frame_buf), &frame_len) == FAAM_OK) {
            uint8_t adts[7];
            int aot = 2; /* AAC LC default */
            int profile = aot - 1;
            int frame_length = frame_len + 7;

            adts[0] = 0xFF;
            adts[1] = 0xF1;
            adts[2] = (uint8_t)((profile << 6) | ((sr_idx & 0xF) << 2) | ((tinfo.channels >> 2) & 0x1));
            adts[3] = (uint8_t)(((tinfo.channels & 0x3) << 6) | ((frame_length >> 11) & 0x3));
            adts[4] = (uint8_t)((frame_length >> 3) & 0xFF);
            adts[5] = (uint8_t)(((frame_length & 0x7) << 5) | 0x1F);
            adts[6] = 0xFC;

            fwrite(adts, 1, 7, fadts);
            fwrite(frame_buf, 1, frame_len, fadts);
        }
        fclose(fadts);
        faam_demuxer_close(&demuxer);
        fclose(fin);
        decode_input = tmp_adts;
    } else {
        fclose(fin);
    }

    /* Destination raw WAV before trimming */
    char raw_wav[1024];
    if (is_mp4 && (gapless.encoder_delay > 0 || gapless.end_padding > 0)) {
        snprintf(raw_wav, sizeof(raw_wav), "%s.raw.%d.wav", out_wav, (int)getpid());
    } else {
        snprintf(raw_wav, sizeof(raw_wav), "%s", out_wav);
    }

    /* Build args for in-process libxaac decoder call */
    char **core_argv = malloc((argc + 4) * sizeof(char *));
    core_argv[0] = argv[0];
    int cidx = 1;
    for (int i = 1; i < argc; i++) {
        if (!strncmp(argv[i], "-ifile:", 7)) {
            char *tmp_arg = NULL;
            if (asprintf(&tmp_arg, "-ifile:%s", decode_input) > 0) {
                core_argv[cidx++] = tmp_arg;
            }
        } else if (!strncmp(argv[i], "-ofile:", 7)) {
            char *tmp_arg = NULL;
            if (asprintf(&tmp_arg, "-ofile:%s", raw_wav) > 0) {
                core_argv[cidx++] = tmp_arg;
            }
        } else {
            core_argv[cidx++] = argv[i];
        }
    }
    core_argv[cidx] = NULL;

    /* Initialize decoder global state */
    ia_testbench_error_handler_init();
    g_pf_inp = FileWrapper_Open((char *)decode_input);
    if (!g_pf_inp) {
        fprintf(stderr, "Failed to open decode input: %s\n", decode_input);
        if (is_mp4) unlink(tmp_adts);
        return 1;
    }
    g_pf_out = fopen(raw_wav, "wb");
    if (!g_pf_out) {
        perror("Failed to open output WAV");
        FileWrapper_Close(g_pf_inp);
        if (is_mp4) unlink(tmp_adts);
        return 1;
    }
    g_w_malloc_count = 0;
    raw_testing = 0;
    eld_testing = 0;
    mpeg_d_drc_on = 0;

    int ret = ixheaacd_main_process(cidx - 1, (pWORD8 *)(&core_argv[1]));

    for (int i = 0; i < g_w_malloc_count; i++) {
        if (g_pv_arr_alloc_memory[i]) free(g_pv_arr_alloc_memory[i]);
    }
    if (g_pf_out) { fclose(g_pf_out); g_pf_out = NULL; }
    if (g_pf_meta) { fclose(g_pf_meta); g_pf_meta = NULL; metadata_mp4_stsz_size_free(&meta_info); }
    if (g_pf_inp) { FileWrapper_Close(g_pf_inp); g_pf_inp = NULL; }

    if (is_mp4) {
        unlink(tmp_adts);
    }

    if (ret != 0) {
        fprintf(stderr, "libxaac decoding failed with code %d\n", ret);
        unlink(raw_wav);
        return 1;
    }

    /* Apply gapless trimming if needed */
    if (is_mp4 && (gapless.encoder_delay > 0 || gapless.end_padding > 0)) {
        trim_wav(raw_wav, out_wav, gapless.encoder_delay, gapless.end_padding);
    }

    return 0;
}
