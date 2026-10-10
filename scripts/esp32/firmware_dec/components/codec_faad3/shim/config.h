/* Stand-in for the Meson-generated config.h of the FAAD3 tree. */
#pragma once
#define PACKAGE "faac"
#define PACKAGE_VERSION "3.0.0"
#define WORDS_BIGENDIAN 0
#ifndef MAX_CHANNELS
#define MAX_CHANNELS 2
#endif
#define FAAC_SBR_DECIMATION 0
#define FAAC_STATS 0
#define FAAD_STATS 0

#ifdef CODEC_PSRAM_STATE
/* libfaad allocates through these macros (faad_internal.h: "overridable, e.g. for embedded PSRAM"). */
#include <stddef.h>
void *codec_faad3_alloc(size_t size);
void codec_faad3_free(void *block);
#define AllocMemory(size) codec_faad3_alloc(size)
#define FreeMemory(block) codec_faad3_free(block)
#endif
