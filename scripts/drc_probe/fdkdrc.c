/* Oracle: decode ADTS with libfdk-aac DRC parameters set, 16-bit WAV out.
 * usage: fdkdrc in.aac out.wav cut(0..127) boost(0..127) target(0=off, else 40..127) */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "aacdecoder_lib.h"
static void le(uint8_t *p, uint32_t v, int n) { for (int i = 0; i < n; i++) p[i] = v >> (8 * i); }
int main(int c, char **v) {
  if (c < 6) return 2;
  FILE *f = fopen(v[1], "rb"); fseek(f, 0, SEEK_END); long len = ftell(f); fseek(f, 0, SEEK_SET);
  UCHAR *buf = malloc(len); fread(buf, 1, len, f); fclose(f);
  HANDLE_AACDECODER d = aacDecoder_Open(TT_MP4_ADTS, 1);
  aacDecoder_SetParam(d, AAC_PCM_LIMITER_ENABLE, 0);
  aacDecoder_SetParam(d, AAC_DRC_ATTENUATION_FACTOR, atoi(v[3]));
  aacDecoder_SetParam(d, AAC_DRC_BOOST_FACTOR, atoi(v[4]));
  if (atoi(v[5])) aacDecoder_SetParam(d, AAC_DRC_REFERENCE_LEVEL, atoi(v[5]));
  else aacDecoder_SetParam(d, AAC_DRC_REFERENCE_LEVEL, -1);
  size_t cap = 1 << 22, n = 0; int16_t *pcm = malloc(cap * 2); INT_PCM out[8 * 2048 * 2];
  int ch = 2, sr = 48000; UCHAR *p = buf; UINT size = len, valid = len;
  while (size > 0) {
    if (aacDecoder_Fill(d, &p, &size, &valid) != AAC_DEC_OK) break;
    UINT used = size - valid; p += used; size = valid;
    for (;;) {
      if (aacDecoder_DecodeFrame(d, out, sizeof out / sizeof out[0], 0) != AAC_DEC_OK) break;
      CStreamInfo *si = aacDecoder_GetStreamInfo(d); ch = si->numChannels; sr = si->sampleRate;
      size_t k = (size_t)si->frameSize * ch; if (n + k > cap) { cap = (n + k) * 2; pcm = realloc(pcm, cap * 2); }
      memcpy(pcm + n, out, k * 2); n += k;
    }
  }
  FILE *o = fopen(v[2], "wb"); uint8_t h[44]; memcpy(h, "RIFF", 4); le(h + 4, 36 + n * 2, 4); memcpy(h + 8, "WAVEfmt ", 8);
  le(h + 16, 16, 4); le(h + 20, 1, 2); le(h + 22, ch, 2); le(h + 24, sr, 4); le(h + 28, sr * ch * 2, 4); le(h + 32, ch * 2, 2); le(h + 34, 16, 2);
  memcpy(h + 36, "data", 4); le(h + 40, n * 2, 4); fwrite(h, 1, 44, o); fwrite(pcm, 2, n, o); fclose(o); return 0; }
