#pragma once
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int out_rate;      /* output sample rate in Hz (after SBR upsampling) */
    int out_channels;
    int out_samples;   /* total interleaved int16 values written to pcm */
} codec_frame_info_t;

/* Largest pcm buffer a codec may write per frame, in int16 values
 * (2 channels x 2048 samples: SBR-upsampled long frame). */
#define CODEC_PCM_MAX_SAMPLES 4096

/* 0 on success. */
int codec_init(void);

/* Decode one frame from an ADTS stream. The adapter searches for the sync
 * word itself and always advances *in / *bytes_left by at least one byte
 * unless *bytes_left is already <= 0.
 * Returns 0 = frame decoded (fi filled), >0 = nothing produced (no sync /
 * truncated tail / empty frame), <0 = decode error. */
int codec_decode_frame(const uint8_t **in, int *bytes_left, int16_t *pcm,
                       codec_frame_info_t *fi);

void codec_close(void);

#ifdef __cplusplus
}
#endif
