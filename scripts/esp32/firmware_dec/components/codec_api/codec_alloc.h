#pragma once
#include "esp_heap_caps.h"

/* Where a decoder's own allocations live: internal RAM by default, PSRAM with -DCODEC_PSRAM=1. */
#ifdef CODEC_PSRAM_STATE
#define CODEC_ALLOC_CAPS (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)
#else
#define CODEC_ALLOC_CAPS (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)
#endif
