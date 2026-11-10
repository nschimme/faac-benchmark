/*
 * xaacenc - Ittiam libxaac M4A Encoder Wrapper using libfaam
 *
 * Links libxaacenc directly (in-process) and muxes frames directly into
 * an MPEG-4 (.m4a) container with gapless (iTunSMPB / edit list) metadata
 * using the libfaam C API. No child processes, no fork/exec.
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <unistd.h>
#include <libgen.h>

#include "ixheaac_type_def.h"
#include "ixheaace_api.h"
#include "ixheaac_error_standards.h"
#include "ixheaace_error_handler.h"
#include "faam.h"

/* Structure matching ixheaace_testbench application context */
typedef struct {
  FILE *pf_inp;
  FILE *pf_out;
  FILE *pf_meta;
  WORD32 use_ga_hdr;
} ixheaace_app_context;

/* Declared in libxaac testbench */
IA_ERRORCODE ia_enhaacplus_enc_main_process(ixheaace_app_context *pstr_context, WORD32 argc, pWORD8 argv[]);
VOID ia_enhaacplus_enc_print_usage(void);
VOID ia_enhaacplus_enc_display_id_message(pWORD8 p_lib_name, pWORD8 p_version_num);

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

static uint32_t get_sr_index(uint32_t sr) {
    static const uint32_t srs[] = {
        96000, 88200, 64000, 48000, 44100, 32000,
        24000, 22050, 16000, 12000, 11025, 8000, 7350
    };
    for (uint32_t i = 0; i < sizeof(srs)/sizeof(srs[0]); i++) {
        if (srs[i] == sr) return i;
    }
    return 4; /* default 44100 */
}

int main(int argc, char **argv) {
    const char *in_wav = NULL;
    const char *out_m4a = NULL;
    int aot = 2;
    bool is_usac = false;

    for (int i = 1; i < argc; i++) {
        if (!strncmp(argv[i], "-ifile:", 7)) {
            in_wav = argv[i] + 7;
        } else if (!strncmp(argv[i], "-ofile:", 7)) {
            out_m4a = argv[i] + 7;
        } else if (!strncmp(argv[i], "-aot:", 5)) {
            aot = atoi(argv[i] + 5);
        } else if (!strncmp(argv[i], "-usac:", 6)) {
            is_usac = (atoi(argv[i] + 6) != 0);
        } else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "-help") || !strcmp(argv[i], "-v") || !strcmp(argv[i], "--version")) {
            ixheaace_version ver = {0};
            ixheaace_get_lib_id_strings(&ver);
            ia_enhaacplus_enc_display_id_message(ver.p_lib_name, ver.p_version_num);
            ia_enhaacplus_enc_print_usage();
            return 0;
        }
    }

    if (!in_wav || !out_m4a) {
        fprintf(stderr, "Usage: %s -ifile:<in.wav> -ofile:<out.m4a> [libxaac args]\n", argv[0]);
        return 1;
    }

    /* Check if target output is .m4a or raw .aac */
    bool output_is_m4a = true;
    size_t out_len = strlen(out_m4a);
    if (out_len > 4 && strcasecmp(out_m4a + out_len - 4, ".aac") == 0) {
        output_is_m4a = false;
    }

    /* Parse input WAV header to determine sample rate, channels, and PCM length */
    FILE *fw = fopen(in_wav, "rb");
    if (!fw) {
        perror("Failed to open input WAV");
        return 1;
    }
    uint8_t wav_hdr[44];
    if (fread(wav_hdr, 1, 44, fw) < 44 || memcmp(wav_hdr, "RIFF", 4) != 0 || memcmp(wav_hdr + 8, "WAVE", 4) != 0) {
        fprintf(stderr, "Invalid or non-PCM WAV header in %s\n", in_wav);
        fclose(fw);
        return 1;
    }
    uint16_t num_channels = wav_hdr[22] | (wav_hdr[23] << 8);
    uint32_t sample_rate = wav_hdr[24] | (wav_hdr[25] << 8) | (wav_hdr[26] << 16) | (wav_hdr[27] << 24);
    uint16_t bits_per_sample = wav_hdr[34] | (wav_hdr[35] << 8);

    /* Scan for data chunk */
    uint32_t data_bytes = 0;
    fseek(fw, 12, SEEK_SET);
    while (!feof(fw)) {
        char chunk_id[4];
        uint32_t chunk_sz = 0;
        if (fread(chunk_id, 1, 4, fw) < 4) break;
        if (fread(&chunk_sz, 1, 4, fw) < 4) break;
        if (memcmp(chunk_id, "data", 4) == 0) {
            data_bytes = chunk_sz;
            break;
        }
        fseek(fw, (chunk_sz + 1) & ~1, SEEK_CUR);
    }
    fclose(fw);

    uint64_t total_pcm_samples = 0;
    if (bits_per_sample > 0 && num_channels > 0) {
        total_pcm_samples = data_bytes / (num_channels * (bits_per_sample / 8));
    }

    /* Destination bitstream file */
    char tmp_adts[1024];
    if (output_is_m4a) {
        snprintf(tmp_adts, sizeof(tmp_adts), "%s.tmp.%d.adts", out_m4a, (int)getpid());
    } else {
        snprintf(tmp_adts, sizeof(tmp_adts), "%s", out_m4a);
    }

    /* Build argument list for libxaac encoder */
    char **core_argv = malloc((argc + 4) * sizeof(char *));
    core_argv[0] = argv[0];
    int cidx = 1;
    bool has_adts_flag = false;
    for (int i = 1; i < argc; i++) {
        if (!strncmp(argv[i], "-ofile:", 7)) {
            char *tmp_arg = NULL;
            if (asprintf(&tmp_arg, "-ofile:%s", tmp_adts) > 0) {
                core_argv[cidx++] = tmp_arg;
            }
        } else {
            core_argv[cidx++] = argv[i];
            if (!strncmp(argv[i], "-adts:", 6)) has_adts_flag = true;
        }
    }
    if (!has_adts_flag && !is_usac && aot != 42) {
        core_argv[cidx++] = "-adts:1";
    }
    core_argv[cidx] = NULL;

    ixheaace_app_context ctx;
    memset(&ctx, 0, sizeof(ctx));
    ctx.use_ga_hdr = 1;
    for (int i = 1; i < cidx; i++) {
        if (!strncmp(core_argv[i], "-adts:", 6)) {
            if (atoi(core_argv[i] + 6) != 0) ctx.use_ga_hdr = 0;
        }
    }

    ctx.pf_inp = fopen(in_wav, "rb");
    if (!ctx.pf_inp) {
        perror("Failed to open input WAV for libxaac");
        return 1;
    }
    ctx.pf_out = fopen(tmp_adts, "wb");
    if (!ctx.pf_out) {
        perror("Failed to open output bitstream for libxaac");
        fclose(ctx.pf_inp);
        return 1;
    }

    IA_ERRORCODE err = ia_enhaacplus_enc_main_process(&ctx, cidx, (pWORD8 *)core_argv);
    if (ctx.pf_inp) fclose(ctx.pf_inp);
    if (ctx.pf_out) fclose(ctx.pf_out);
    if (ctx.pf_meta) {
        fclose(ctx.pf_meta);
        ctx.pf_meta = NULL;
    }

    if (err != IA_NO_ERROR) {
        fprintf(stderr, "libxaac encoding error: %d\n", (int)err);
        unlink(tmp_adts);
        return 1;
    }

    if (!output_is_m4a) {
        return 0;
    }

    /* Mux ADTS stream to M4A using libfaam directly */
    FILE *fadts = fopen(tmp_adts, "rb");
    if (!fadts) {
        perror("Failed to open intermediate ADTS bitstream");
        return 1;
    }

    FILE *fm4a = fopen(out_m4a, "w+b");
    if (!fm4a) {
        perror("Failed to open output M4A file");
        fclose(fadts);
        unlink(tmp_adts);
        return 1;
    }

    faam_io io = make_file_io(fm4a);

    /* In HE-AAC (AOT 5) and HE-AAC v2 (AOT 29), the core coder runs at half the input sample rate */
    uint32_t core_rate = (aot == 5 || aot == 29) ? (sample_rate / 2) : sample_rate;
    uint32_t sr_idx = get_sr_index(core_rate);

    /* Construct AudioSpecificConfig (base AAC-LC at core rate) */
    uint8_t asc[64];
    uint32_t asc_len = 0;
    asc[0] = (uint8_t)((2 << 3) | ((sr_idx >> 1) & 0x7));
    asc[1] = (uint8_t)(((sr_idx & 0x1) << 7) | ((num_channels & 0xF) << 3));
    asc_len = 2;

    /* Priming / delay setup: libxaac introduces 1600 core samples for AAC-LC */
    uint32_t encoder_delay = (aot == 5 || aot == 29) ? 4165 : 1600;

    faam_gapless_info gapless;
    memset(&gapless, 0, sizeof(gapless));
    gapless.struct_size = sizeof(gapless);
    gapless.encoder_delay = encoder_delay;
    gapless.total_samples = total_pcm_samples;
    gapless.end_padding = 0;

    faam_track_config track;
    faam_track_config_init(&track, sizeof(track));
    track.codec_id = FAAM_CODEC_AAC;
    track.track_type = FAAM_TRACK_AUDIO;
    track.timescale = core_rate;
    track.sample_rate = core_rate;
    track.channels = num_channels;
    track.codec_data = asc;
    track.codec_data_len = asc_len;
    track.gapless = &gapless;

    faam_muxer_config mux_cfg;
    faam_muxer_config_init(&mux_cfg, sizeof(mux_cfg));
    mux_cfg.flags = FAAM_MUXER_FASTSTART;
    mux_cfg.num_tracks = 1;
    mux_cfg.tracks = &track;
    mux_cfg.gapless = &gapless;

    faam_muxer *muxer = NULL;
    faam_status st = faam_muxer_open(&mux_cfg, &io, &muxer);
    if (st != FAAM_OK) {
        fprintf(stderr, "libfaam faam_muxer_open failed: %s\n", faam_strerror(st));
        fclose(fadts);
        fclose(fm4a);
        unlink(tmp_adts);
        return 1;
    }

    uint32_t track_id = 0;
    faam_muxer_get_track_id(muxer, 0, &track_id);

    /* Read ADTS frames and write to muxer */
    uint8_t fbuf[8192];
    uint32_t frames_written = 0;
    while (!feof(fadts)) {
        uint8_t hdr[7];
        if (fread(hdr, 1, 7, fadts) < 7) break;
        if (hdr[0] != 0xFF || (hdr[1] & 0xF0) != 0xF0) {
            break;
        }
        int protection_absent = hdr[1] & 0x01;
        int frame_length = ((hdr[3] & 0x03) << 11) | (hdr[4] << 3) | ((hdr[5] & 0xE0) >> 5);
        int hdr_len = protection_absent ? 7 : 9;
        if (!protection_absent) {
            uint8_t crc[2];
            if (fread(crc, 1, 2, fadts) < 2) break;
        }

        int payload_len = frame_length - hdr_len;
        if (payload_len <= 0 || payload_len > (int)sizeof(fbuf)) break;

        if (fread(fbuf, 1, payload_len, fadts) < (size_t)payload_len) break;

        uint32_t duration_ticks = 1024;
        st = faam_muxer_write_frame(muxer, track_id, fbuf, payload_len, duration_ticks, 0, FAAM_FRAME_KEYFRAME);
        if (st != FAAM_OK) {
            fprintf(stderr, "faam_muxer_write_frame error: %s\n", faam_strerror(st));
            break;
        }
        frames_written++;
    }
    fclose(fadts);
    unlink(tmp_adts);

    if (total_pcm_samples > 0 && frames_written > 0) {
        uint64_t total_decoded_samples = (uint64_t)frames_written * 1024;
        if (total_decoded_samples > total_pcm_samples + encoder_delay) {
            gapless.end_padding = (uint32_t)(total_decoded_samples - (total_pcm_samples + encoder_delay));
            faam_muxer_update_params params;
            memset(&params, 0, sizeof(params));
            params.struct_size = sizeof(params);
            params.flags = FAAM_UPDATE_GAPLESS;
            params.track_id = track_id;
            params.gapless = &gapless;
            faam_muxer_update(muxer, &params);
        }
    }

    st = faam_muxer_finalize(muxer);
    if (st != FAAM_OK) {
        fprintf(stderr, "faam_muxer_finalize error: %s\n", faam_strerror(st));
    }
    faam_muxer_close(&muxer);
    fclose(fm4a);

    return (st == FAAM_OK) ? 0 : 1;
}
