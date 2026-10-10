#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "esp_heap_caps.h"
#include "esp_rom_sys.h"
#include "codec_alloc.h"
#include "faad.h"
#include "codec_api.h"

#ifdef CODEC_PSRAM_STATE
void *codec_faad3_alloc(size_t size) { return heap_caps_malloc(size, CODEC_ALLOC_CAPS); }
void codec_faad3_free(void *block) { heap_caps_free(block); }
#endif

static faad_decoder *s_dec;
static void *s_state;   /* caller-owned decoder state */
static void *s_pcm;     /* FAAD3 writes up to max_output_bytes per call */
static uint32_t s_pcm_cap;

#ifdef CODEC_PROFILE
/* Exclusive-cycle profiler driven by -finstrument-functions (see CMakeLists.txt). */
#include "esp_cpu.h"
#define PROF_SLOTS 96
#define PROF_DEPTH 48
static struct { void *fn; uint64_t cycles; uint32_t calls; } s_prof[PROF_SLOTS];
static void *s_stack[PROF_DEPTH];
static int s_sp;
static uint32_t s_last;

static void __attribute__((no_instrument_function)) prof_charge(void *fn, uint32_t d)
{
    for (int i = 0; i < PROF_SLOTS; i++) {
        if (s_prof[i].fn == fn || !s_prof[i].fn) {
            s_prof[i].fn = fn;
            s_prof[i].cycles += d;
            return;
        }
    }
}

void __attribute__((no_instrument_function)) __cyg_profile_func_enter(void *fn, void *cs)
{
    uint32_t now = esp_cpu_get_cycle_count();
    if (s_sp > 0 && s_sp <= PROF_DEPTH)
        prof_charge(s_stack[s_sp - 1], now - s_last);
    if (s_sp < PROF_DEPTH)
        s_stack[s_sp] = fn;
    s_sp++;
    for (int i = 0; i < PROF_SLOTS; i++) {
        if (s_prof[i].fn == fn || !s_prof[i].fn) { s_prof[i].fn = fn; s_prof[i].calls++; break; }
    }
    s_last = esp_cpu_get_cycle_count();
}

void __attribute__((no_instrument_function)) __cyg_profile_func_exit(void *fn, void *cs)
{
    uint32_t now = esp_cpu_get_cycle_count();
    if (s_sp > 0 && s_sp <= PROF_DEPTH)
        prof_charge(s_stack[s_sp - 1], now - s_last);
    s_sp--;
    s_last = esp_cpu_get_cycle_count();
}

static void prof_report(void)
{
    for (int i = 0; i < PROF_SLOTS && s_prof[i].fn; i++)
        esp_rom_printf("PROF %08x %08x%08x %u\n", (unsigned)s_prof[i].fn, (unsigned)(s_prof[i].cycles >> 32),
                       (unsigned)s_prof[i].cycles, (unsigned)s_prof[i].calls);
    esp_rom_printf("PROFEND\n");
    memset(s_prof, 0, sizeof(s_prof));
    s_sp = 0;
}
#else
#define prof_report() ((void)0)
#endif

int codec_init(void)
{
    faad_config cfg;
    if (faad_config_init(&cfg, sizeof(cfg)) != FAAD_OK)
        return -1;
    cfg.stream_format = FAAD_STREAM_ADTS;
    cfg.output_format = FAAD_OUTPUT_16BIT;

#ifdef CODEC_FAAD3_OPEN
    /* The library allocates its state as several independent blocks. */
    faad_status st = faad_decoder_open(&cfg, NULL, 0, &s_dec);
    if (st != FAAD_OK) {
        esp_rom_printf("faad3: faad_decoder_open: %s (largest block %u)\n", faad_strerror(st),
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
        codec_close();
        return -1;
    }
#else
    uint32_t state_bytes = 0;
    if (faad_get_state_size(&state_bytes) != FAAD_OK)
        return -1;
    s_state = heap_caps_aligned_alloc(FAAD_STATE_ALIGNMENT, state_bytes, CODEC_ALLOC_CAPS);
    if (!s_state || faad_decoder_init(s_state, state_bytes, &cfg, NULL, 0, &s_dec) != FAAD_OK) {
        codec_close();
        return -1;
    }
#endif
    faad_stream_info info = { .struct_size = sizeof(info) };
    if (faad_decoder_get_info(s_dec, &info) != FAAD_OK) {
        codec_close();
        return -1;
    }
    s_pcm_cap = info.max_output_bytes;
    s_pcm = heap_caps_aligned_alloc(16, s_pcm_cap, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) /* scratch stays internal */;
    if (!s_pcm) {
        esp_rom_printf("faad3: pcm scratch %u B failed (largest block %u)\n", (unsigned)s_pcm_cap,
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
        codec_close();
        return -1;
    }
#ifdef CODEC_PROFILE
    memset(s_prof, 0, sizeof(s_prof)); /* count the decode calls only, not the open */
    s_sp = 0;
#endif
    return 0;
}

void codec_close(void)
{
    prof_report();
#ifdef CODEC_FAAD3_OPEN
    faad_decoder_close(&s_dec);
#endif
    s_dec = NULL; /* single-block state is caller-owned: freed below */
    heap_caps_free(s_state);
    heap_caps_free(s_pcm);
    s_state = s_pcm = NULL;
}

int codec_decode_frame(const uint8_t **in, int *bytes_left, int16_t *pcm,
                       codec_frame_info_t *fi)
{
    if (!s_dec || *bytes_left <= 0)
        return 1;

    uint32_t used = 0, written = 0, flags = 0;
    faad_status st = faad_decode_frame(s_dec, *in, (uint32_t)*bytes_left, &used,
                                       s_pcm, s_pcm_cap, &written, &flags);
    if (used == 0)
        used = (st == FAAD_ERR_NEED_MORE_DATA) ? (uint32_t)*bytes_left : 1;
    if (used > (uint32_t)*bytes_left)
        used = (uint32_t)*bytes_left;
    *in += used;
    *bytes_left -= (int)used;

    if (st == FAAD_ERR_NEED_MORE_DATA || st == FAAD_ERR_SYNC_LOST)
        return 1;
    if (st != FAAD_OK)
        return -1;
    if (written == 0)
        return 1;
    if (written > CODEC_PCM_MAX_SAMPLES * sizeof(int16_t))
        return -1;

    faad_stream_info info = { .struct_size = sizeof(info) };
    if (faad_decoder_get_info(s_dec, &info) != FAAD_OK)
        return -1;
    memcpy(pcm, s_pcm, written);
    fi->out_rate = (int)info.sample_rate;
    fi->out_channels = (int)info.channels;
    fi->out_samples = (int)(written / sizeof(int16_t));
    return 0;
}
