/*
 * xaacenc: encode WAV to MP4/M4A with libxaac & libfaam.
 * Encodes audio with libxaac and muxes directly via libfaam C API,
 * writing iTunSMPB and elst gapless metadata directly into the MP4 container.
 */

#define _DEFAULT_SOURCE
#define _POSIX_C_SOURCE 200809L
#define _FILE_OFFSET_BITS 64
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <libgen.h>

#include <faam.h>

static int32_t io_write(void *user_data, const void *buf, uint32_t bytes) {
    size_t n = fwrite(buf, 1, bytes, (FILE *)user_data);
    return ferror((FILE *)user_data) || n > INT32_MAX ? -1 : (int32_t)n;
}

static bool io_seek(void *user_data, uint64_t offset) {
    return offset <= INT64_MAX && fseeko((FILE *)user_data, (off_t)offset, SEEK_SET) == 0;
}

static uint64_t io_tell(void *user_data) {
    off_t pos = ftello((FILE *)user_data);
    return pos < 0 ? UINT64_MAX : (uint64_t)pos;
}

static int parse_adts_frame(const uint8_t *buf, size_t len, int *frame_len) {
    if (len < 7) return -1;
    if (buf[0] != 0xff || (buf[1] & 0xf6) != 0xf0) return -1;
    *frame_len = ((buf[3] & 0x03) << 11) | (buf[4] << 3) | ((buf[5] & 0xe0) >> 5);
    return (buf[1] & 1) ? 7 : 9; // header size (without / with CRC)
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
        printf("xaacenc (Ittiam libxaac 2.0)\n");
        return 0;
    }

    char in_path[1024] = {0};
    char out_path[1024] = {0};
    int bitrate_kbps = 128;
    int aot = 2; // 2=LC, 5=HE, 29=HE-v2, 42=USAC

    for (int i = 1; i < argc; i++) {
        if (!strncmp(argv[i], "-ifile:", 7)) strncpy(in_path, argv[i] + 7, sizeof(in_path) - 1);
        else if (!strncmp(argv[i], "-ofile:", 7)) strncpy(out_path, argv[i] + 7, sizeof(out_path) - 1);
        else if (!strncmp(argv[i], "-br:", 4)) bitrate_kbps = atoi(argv[i] + 4) / 1000;
        else if (!strncmp(argv[i], "-aot:", 5)) aot = atoi(argv[i] + 5);
        else if (!strcmp(argv[i], "-b") && i + 1 < argc) bitrate_kbps = atoi(argv[++i]);
        else if (!strcmp(argv[i], "-p") && i + 1 < argc) {
            const char *p = argv[++i];
            if (!strcmp(p, "he")) aot = 5;
            else if (!strcmp(p, "hev2")) aot = 29;
            else if (!strcmp(p, "usac")) aot = 42;
            else aot = 2;
        } else if (argv[i][0] != '-') {
            if (!in_path[0]) strncpy(in_path, argv[i], sizeof(in_path) - 1);
            else if (!out_path[0]) strncpy(out_path, argv[i], sizeof(out_path) - 1);
        }
    }

    if (!in_path[0] || !out_path[0]) {
        fprintf(stderr, "usage: %s -ifile:<in.wav> -ofile:<out.m4a> [-br:<bps>] [-aot:<aot>]\n", argv[0]);
        return 2;
    }

    // Inspect input WAV to obtain total PCM samples for accurate padding calculation
    uint32_t wav_samples = 0;
    FILE *wf = fopen(in_path, "rb");
    if (wf) {
        uint8_t whdr[44];
        if (fread(whdr, 1, 44, wf) == 44) {
            uint16_t wch = *(uint16_t *)(whdr + 22);
            fseeko(wf, 0, SEEK_END);
            off_t wsize = ftello(wf);
            if (wsize > 44 && wch > 0) {
                wav_samples = (uint32_t)(wsize - 44) / (wch * 2);
            }
        }
        fclose(wf);
    }

    bool is_m4a = strstr(out_path, ".m4a") || strstr(out_path, ".mp4");

    char temp_aac[] = "/tmp/xaac_enc_XXXXXX.aac";
    if (is_m4a) {
        int tfd = mkstemps(temp_aac, 4);
        if (tfd >= 0) close(tfd);
    }

    char enc_bin[1024];
    find_sibling_core_bin(argv[0], "xaacenc_core", enc_bin, sizeof(enc_bin));

    char ifile_arg[1100], ofile_arg[1100], br_arg[64], aot_arg[64];
    snprintf(ifile_arg, sizeof(ifile_arg), "-ifile:%s", in_path);
    snprintf(ofile_arg, sizeof(ofile_arg), "-ofile:%s", is_m4a ? temp_aac : out_path);
    snprintf(br_arg, sizeof(br_arg), "-br:%d", bitrate_kbps * 1000);
    snprintf(aot_arg, sizeof(aot_arg), "-aot:%d", aot);

    char *exec_args[8];
    int arg_idx = 0;
    exec_args[arg_idx++] = enc_bin;
    exec_args[arg_idx++] = ifile_arg;
    exec_args[arg_idx++] = ofile_arg;
    exec_args[arg_idx++] = br_arg;
    exec_args[arg_idx++] = aot_arg;
    if (aot != 42) exec_args[arg_idx++] = "-adts:1";
    exec_args[arg_idx] = NULL;

    pid_t child = fork();
    int status = 0;
    if (child == 0) {
        FILE *null = fopen("/dev/null", "wb");
        if (null) {
            dup2(fileno(null), STDOUT_FILENO);
            dup2(fileno(null), STDERR_FILENO);
            fclose(null);
        }
        execvp(enc_bin, exec_args);
        _exit(127);
    } else if (child > 0) {
        waitpid(child, &status, 0);
    } else {
        status = -1;
    }

    if (status != 0 || (is_m4a && access(temp_aac, F_OK) != 0)) {
        if (is_m4a) unlink(temp_aac);
        return 1;
    }

    if (!is_m4a) {
        return 0;
    }

    /* Mux ADTS stream directly using libfaam C API */
    FILE *adts_f = fopen(temp_aac, "rb");
    if (!adts_f) { unlink(temp_aac); return 1; }

    fseeko(adts_f, 0, SEEK_END);
    off_t adts_len = ftello(adts_f);
    fseeko(adts_f, 0, SEEK_SET);

    uint8_t *adts_data = malloc(adts_len ? adts_len : 1);
    if (adts_len && fread(adts_data, 1, adts_len, adts_f) != (size_t)adts_len) {
        free(adts_data);
        fclose(adts_f);
        unlink(temp_aac);
        return 1;
    }
    fclose(adts_f);
    unlink(temp_aac);

    FILE *out_f = fopen(out_path, "wb+");
    if (!out_f) { free(adts_data); return 1; }

    faam_io io = { .struct_size = sizeof(io), .user_data = out_f,
                   .write = io_write, .seek = io_seek, .tell = io_tell };

    int first_frame_len = 0;
    int hdr_len = parse_adts_frame(adts_data, adts_len, &first_frame_len);
    if (hdr_len <= 0) {
        free(adts_data);
        fclose(out_f);
        return 1;
    }

    int sr_idx = (adts_data[2] >> 2) & 0x0f;
    int ch = ((adts_data[2] & 1) << 2) | ((adts_data[3] >> 6) & 3);
    uint32_t rates[] = {96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350};
    uint32_t sample_rate = (sr_idx < 13) ? rates[sr_idx] : 44100;

    faam_track_config trk_cfg;
    memset(&trk_cfg, 0, sizeof(trk_cfg));
    trk_cfg.struct_size = sizeof(trk_cfg);
    trk_cfg.track_type = FAAM_TRACK_AUDIO;
    trk_cfg.codec_id = FAAM_CODEC_AAC;
    trk_cfg.timescale = sample_rate;
    trk_cfg.sample_rate = sample_rate;
    trk_cfg.channels = ch ? ch : 2;

    int asc_aot = (aot == 5 || aot == 29) ? 2 : aot;
    uint8_t asc[2];
    asc[0] = (asc_aot << 3) | (sr_idx >> 1);
    asc[1] = ((sr_idx & 1) << 7) | (trk_cfg.channels << 3);
    trk_cfg.codec_data = asc;
    trk_cfg.codec_data_len = 2;

    uint32_t delay = (aot == 5 || aot == 29) ? 2048 : 1024;
    // Calculate accurate padding delay based on input samples and total frame count
    uint32_t num_frames = 0;
    size_t scan_off = 0;
    while (scan_off + 7 <= (size_t)adts_len) {
        int fsz = 0;
        int hsz = parse_adts_frame(adts_data + scan_off, adts_len - scan_off, &fsz);
        if (hsz <= 0 || fsz <= hsz || scan_off + fsz > (size_t)adts_len) break;
        num_frames++;
        scan_off += fsz;
    }

    uint32_t total_coded_samples = num_frames * 1024;
    uint32_t padding = 0;
    if (wav_samples > 0 && total_coded_samples >= (wav_samples + delay)) {
        padding = total_coded_samples - (wav_samples + delay);
    } else {
        padding = 1024;
    }

    faam_gapless_info gapless = { .struct_size = sizeof(gapless),
                                  .encoder_delay = delay,
                                  .end_padding = padding };

    faam_muxer_config mux_cfg;
    faam_muxer_config_init(&mux_cfg, sizeof(mux_cfg));
    mux_cfg.num_tracks = 1;
    mux_cfg.tracks = &trk_cfg;
    mux_cfg.gapless = &gapless;

    faam_muxer *muxer = NULL;
    if (faam_muxer_open(&mux_cfg, &io, &muxer) != FAAM_OK) {
        free(adts_data);
        fclose(out_f);
        return 1;
    }

    uint32_t track_id = 1;
    faam_muxer_get_track_id(muxer, 0, &track_id);

    size_t offset = 0;
    while (offset + 7 <= (size_t)adts_len) {
        int frame_size = 0;
        int h_size = parse_adts_frame(adts_data + offset, adts_len - offset, &frame_size);
        if (h_size <= 0 || frame_size <= h_size || offset + frame_size > (size_t)adts_len) {
            break;
        }

        const uint8_t *raw_frame = adts_data + offset + h_size;
        uint32_t raw_len = frame_size - h_size;

        faam_muxer_write_frame(muxer, track_id, raw_frame, raw_len, 1024, 0, FAAM_FRAME_KEYFRAME);
        offset += frame_size;
    }

    faam_muxer_finalize(muxer);
    faam_muxer_close(&muxer);
    fclose(out_f);
    free(adts_data);

    return 0;
}
