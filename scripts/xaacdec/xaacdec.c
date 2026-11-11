/*
 * xaacdec - Ittiam libxaac M4A Decoder Wrapper using libfaam
 *
 * Uses libfaam to demux MP4/M4A containers into AAC frames, extracts
 * gapless info (priming delay and padding), runs libxaac decoder entirely
 * in-memory, and writes sample-accurate gapless WAV output directly without
 * intermediate temporary disk files.
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

static uint32_t le32(const uint8_t *p) {
    return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void trim_and_save_wav(const uint8_t *raw_buf, size_t raw_sz, const char *final_wav,
                              uint32_t delay_samples, uint32_t padding_samples, uint64_t keep_hint) {
    FILE *out = fopen(final_wav, "wb");
    if (!out) return;

    /* Find the format and data chunks: more than two channels get a longer header. */
    size_t data_off = 0;
    uint32_t orig_bytes = 0;
    uint16_t channels = 0, bits_per_sample = 0;
    if (raw_sz >= 12 && memcmp(raw_buf, "RIFF", 4) == 0 && memcmp(raw_buf + 8, "WAVE", 4) == 0) {
        size_t pos = 12;
        while (pos + 8 <= raw_sz) {
            uint32_t csz = le32(raw_buf + pos + 4);
            if (memcmp(raw_buf + pos, "fmt ", 4) == 0 && pos + 24 <= raw_sz) {
                channels = (uint16_t)(raw_buf[pos + 10] | (raw_buf[pos + 11] << 8));
                bits_per_sample = (uint16_t)(raw_buf[pos + 22] | (raw_buf[pos + 23] << 8));
            } else if (memcmp(raw_buf + pos, "data", 4) == 0) {
                data_off = pos + 8;
                orig_bytes = csz;
                break;
            }
            pos += 8 + (((size_t)csz + 1) & ~(size_t)1);
        }
    }

    /* libxaac sizes the chunks as if the header were 44 bytes, so with more channels the data
     * chunk claims more than the file holds: trust the bytes that are there. */
    if (data_off > 0 && data_off + orig_bytes > raw_sz)
        orig_bytes = (uint32_t)(raw_sz - data_off);

    uint32_t bytes_per_sample = (bits_per_sample / 8) * channels;
    if (data_off == 0 || bytes_per_sample == 0) {
        fwrite(raw_buf, 1, raw_sz, out);
        fclose(out);
        return;
    }

    uint32_t total_samples = orig_bytes / bytes_per_sample;
    if (delay_samples + padding_samples >= total_samples) {
        fwrite(raw_buf, 1, raw_sz, out);
        fclose(out);
        return;
    }

    uint32_t keep_samples = total_samples - delay_samples - padding_samples;
    /* The container's unpadded length, when it has one, is exact; a short decode keeps what it has. */
    if (keep_hint > 0 && keep_hint < (uint64_t)(total_samples - delay_samples))
        keep_samples = (uint32_t)keep_hint;
    uint32_t keep_bytes = keep_samples * bytes_per_sample;

    /* Keep the header as written and update the two sizes */
    uint8_t *hdr = malloc(data_off);
    if (!hdr) {
        fclose(out);
        return;
    }
    memcpy(hdr, raw_buf, data_off);
    uint32_t riff_size = (uint32_t)(data_off - 8) + keep_bytes;
    for (int k = 0; k < 4; k++) {
        hdr[4 + k] = (uint8_t)(riff_size >> (8 * k));
        hdr[data_off - 4 + k] = (uint8_t)(keep_bytes >> (8 * k));
    }
    fwrite(hdr, 1, data_off, out);
    free(hdr);

    /* Write trimmed PCM samples */
    const uint8_t *pcm_start = raw_buf + data_off + ((size_t)delay_samples * bytes_per_sample);
    fwrite(pcm_start, 1, keep_bytes, out);
    fclose(out);
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
    uint32_t core_rate_hz = 0; /* the container's sample unit; the output is twice that for SBR */
    bool sbr_signalled = false; /* the configuration says SBR (and PS) are present */

    char *adts_mem_buf = NULL;
    size_t adts_mem_sz = 0;
    FILE *f_adts_mem = NULL;

    if (is_mp4) {
        /* Input is MP4/M4A: demux in-memory using libfaam directly */
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
        core_rate_hz = tinfo.sample_rate;

        /* Open in-memory buffer stream for demuxed ADTS */
        f_adts_mem = open_memstream(&adts_mem_buf, &adts_mem_sz);
        if (!f_adts_mem) {
            perror("Failed to open memstream for ADTS frames");
            faam_demuxer_close(&demuxer);
            fclose(fin);
            return 1;
        }

        /* Read track's AudioSpecificConfig (codec_data) to determine the exact core rate & profile */
        uint8_t cdata[64];
        uint32_t clen = 0;
        int aot = 2;
        int sr_idx = -1;
        int asc_channels = -1;

        if (faam_demuxer_get_codec_data(demuxer, 1, cdata, sizeof(cdata), &clen) == FAAM_OK && clen >= 2) {
            aot = (cdata[0] >> 3) & 0x1F;
            sr_idx = ((cdata[0] & 0x7) << 1) | ((cdata[1] >> 7) & 0x1);
            asc_channels = (cdata[1] >> 3) & 0xF;
            /* Backward-compatible signalling follows the core config: sync extension 0x2b7 */
            sbr_signalled = aot == 5 || aot == 29 ||
                            (clen >= 4 && (((cdata[2] << 3) | (cdata[3] >> 5)) & 0x7ff) == 0x2b7);
        }

        if (sr_idx < 0 || sr_idx > 12) {
            static const uint32_t srs[] = {
                96000, 88200, 64000, 48000, 44100, 32000,
                24000, 22050, 16000, 12000, 11025, 8000, 7350
            };
            sr_idx = 4;
            for (int i = 0; i < 13; i++) {
                if (srs[i] == tinfo.sample_rate) { sr_idx = i; break; }
            }
        }

        uint8_t frame_buf[8192];
        uint32_t frame_len = 0;

        uint8_t last_frame[8192];
        uint32_t last_len = 0;
        int flush_left = 0, flushed = 0;
        for (;;) {
            if (flush_left > 0) {
                /* Replay the last frame: the decoder holds back its final delay, and the
                 * output past the container's unpadded length is cut afterwards. */
                memcpy(frame_buf, last_frame, last_len);
                frame_len = last_len;
                flush_left--;
            } else if (faam_demuxer_read_frame(demuxer, frame_buf, sizeof(frame_buf), &frame_len) != FAAM_OK) {
                if (!flushed && last_len > 0 && gapless.total_samples > 0) {
                    flushed = 1;
                    flush_left = 2;
                    continue;
                }
                break;
            }
            uint8_t adts[7];
            int aot = 2; /* AAC LC default */
            int profile = aot - 1;
            int frame_length = frame_len + 7;

            /* In AAC elementary bitstreams, determine core channels from the first syntax element:
             * Element ID 0 = ID_SCE (Single Channel Element / Mono / HE-v2 Parametric Stereo) -> 1 ch
             * Element ID 1 = ID_CPE (Channel Pair Element / Stereo) -> 2 ch */
            int ch = tinfo.channels;
            if (frame_len > 0) {
                int elem_id = (frame_buf[0] >> 5) & 7;
                if (elem_id == 0) ch = 1;
                else if (elem_id == 1) ch = 2;
            }

            adts[0] = 0xFF;
            adts[1] = 0xF1;
            adts[2] = (uint8_t)((profile << 6) | ((sr_idx & 0xF) << 2) | ((ch >> 2) & 0x1));
            adts[3] = (uint8_t)(((ch & 0x3) << 6) | ((frame_length >> 11) & 0x3));
            adts[4] = (uint8_t)((frame_length >> 3) & 0xFF);
            adts[5] = (uint8_t)(((frame_length & 0x7) << 5) | 0x1F);
            adts[6] = 0xFC;

            fwrite(adts, 1, 7, f_adts_mem);
            fwrite(frame_buf, 1, frame_len, f_adts_mem);
            if (flush_left == 0 && frame_len > 0 && frame_len <= sizeof(last_frame)) {
                memcpy(last_frame, frame_buf, frame_len);
                last_len = frame_len;
            }
        }
        fclose(f_adts_mem);
        faam_demuxer_close(&demuxer);
        fclose(fin);
    } else {
        fclose(fin);
    }

    /* Build args for in-process libxaac decoder call */
    char **core_argv = malloc((argc + 4) * sizeof(char *));
    core_argv[0] = argv[0];
    int cidx = 1;
    for (int i = 1; i < argc; i++) {
        if (!strncmp(argv[i], "-ifile:", 7)) {
            core_argv[cidx++] = "-ifile:memory_input";
        } else if (!strncmp(argv[i], "-ofile:", 7)) {
            core_argv[cidx++] = "-ofile:memory_output";
        } else {
            core_argv[cidx++] = argv[i];
        }
    }
    core_argv[cidx] = NULL;

    /* Initialize decoder global state */
    ia_testbench_error_handler_init();

    FileWrapperPtr transport = (FileWrapper *)calloc(1, sizeof(FileWrapper));
    transport->isMp4File = 0;
    if (is_mp4) {
        transport->inputFile = fmemopen(adts_mem_buf, adts_mem_sz, "rb");
    } else {
        transport->inputFile = fopen(in_file, "rb");
    }
    g_pf_inp = transport;

    /* In-memory output buffer stream for decoded WAV PCM using preallocated fmemopen buffer */
    size_t wav_mem_cap = 64 * 1024 * 1024; /* 64 MB buffer */
    uint8_t *wav_mem_buf = malloc(wav_mem_cap);
    if (!wav_mem_buf) {
        perror("Failed to allocate in-memory WAV buffer");
        FileWrapper_Close(g_pf_inp);
        if (adts_mem_buf) free(adts_mem_buf);
        return 1;
    }
    FILE *f_wav_mem = fmemopen(wav_mem_buf, wav_mem_cap, "w+b");
    if (!f_wav_mem) {
        perror("Failed to fmemopen decoded WAV buffer");
        free(wav_mem_buf);
        FileWrapper_Close(g_pf_inp);
        if (adts_mem_buf) free(adts_mem_buf);
        return 1;
    }
    g_pf_out = f_wav_mem;

    g_w_malloc_count = 0;
    raw_testing = 0;
    eld_testing = 0;
    mpeg_d_drc_on = 0;

    int ret = ixheaacd_main_process(cidx - 1, (pWORD8 *)(&core_argv[1]));

    for (int i = 0; i < g_w_malloc_count; i++) {
        if (g_pv_arr_alloc_memory[i]) free(g_pv_arr_alloc_memory[i]);
    }

    size_t wav_mem_sz = 0;
    if (g_pf_out) {
        fflush(g_pf_out);
        if (wav_mem_buf[0] == 'R' && wav_mem_buf[1] == 'I' && wav_mem_buf[2] == 'F' && wav_mem_buf[3] == 'F') {
            uint32_t riff_sz = wav_mem_buf[4] | (wav_mem_buf[5] << 8) | (wav_mem_buf[6] << 16) | (wav_mem_buf[7] << 24);
            wav_mem_sz = riff_sz + 8;
        } else {
            long p = ftell(g_pf_out);
            wav_mem_sz = p > 0 ? (size_t)p : 0;
        }
        fclose(g_pf_out);
        g_pf_out = NULL;
    }
    if (g_pf_meta) { fclose(g_pf_meta); g_pf_meta = NULL; metadata_mp4_stsz_size_free(&meta_info); }
    if (g_pf_inp) { FileWrapper_Close(g_pf_inp); g_pf_inp = NULL; }
    if (adts_mem_buf) { free(adts_mem_buf); adts_mem_buf = NULL; }

    if (ret != 0) {
        fprintf(stderr, "libxaac decoding failed with code %d\n", ret);
        if (wav_mem_buf) free(wav_mem_buf);
        return 1;
    }

    /* Apply gapless trimming and write final output WAV directly */
    if (is_mp4 && (gapless.encoder_delay > 0 || gapless.end_padding > 0)) {
        /* Gapless values count samples at the container's core rate; HE-AAC decodes to a multiple of it. */
        uint32_t unit = 1;
        if (core_rate_hz > 0 && wav_mem_sz >= 44) {
            uint32_t out_rate = wav_mem_buf[24] | (wav_mem_buf[25] << 8) | (wav_mem_buf[26] << 16) | ((uint32_t)wav_mem_buf[27] << 24);
            if (out_rate > core_rate_hz && out_rate % core_rate_hz == 0) unit = out_rate / core_rate_hz;
        }
        /* libxaac has no call that reports its output latency. Measured on faac, FDK and
         * libxaac streams at 32, 44.1 and 48 kHz, mono and stereo, it places SBR output 962
         * samples later than the reference decoders the container's priming assumes. */
        const uint32_t sbr_extra = (unit > 1 || sbr_signalled) ? 962 : 0;
        trim_and_save_wav((const uint8_t *)wav_mem_buf, wav_mem_sz, out_wav,
                          gapless.encoder_delay * unit + sbr_extra, gapless.end_padding * unit,
                          gapless.total_samples * unit);
    } else {
        FILE *fout = fopen(out_wav, "wb");
        if (fout && wav_mem_buf) {
            fwrite(wav_mem_buf, 1, wav_mem_sz, fout);
            fclose(fout);
        }
    }

    if (wav_mem_buf) free(wav_mem_buf);
    return 0;
}
