# On-device AAC benchmarks: decoders and the FAAC encoder (ESP32 family)

Decode speed, memory and footprint of AAC decoders on real ESP32 silicon, so the numbers can sit next
to the host `compare_codecs.py` decoder results: **libhelix-aac** and **FAAD2 (`FIXED_POINT`)** (both fixed
point) and the work-in-progress **FAAD3** (float) from the `stack-tests` branch of the faac repo. A second
tool runs the **FAAC encoder** the same way. The **ESP32-S3 is the benchmark target**; the classic ESP32
and the ESP32-C6 only get a stability/smoke run.

**Contents:** [Executive summary](#executive-summary) · [Quick start](#quick-start) ·
[Decoder results](#decoder-results-esp32-s3--240-mhz) · [FAAC encoder](#faac-encoder) · [Classic ESP32 limits](#classic-esp32-limits-no-psram) ·
[Method, builds and versions](#method-builds-and-versions). Long tables and explanations are folded
into collapsible sections; expand them as needed.

## Executive summary

**Is FAAD3 better?** On the ESP32-S3 it beats FAAD2 on speed, flash and stack, with about the same total RAM;
against libhelix it is a trade, not a win; and off the S3 it is the weakest of the three. Measured at 240 MHz
over the gate set (4 clips per scenario, 27 scenarios, FAAC 2.1.0 LC and HE-AAC v1 streams):

| | libhelix (fixed) | FAAD2 (fixed) | FAAD3 (float, WIP) |
|---|---|---|---|
| LC speed, 44.1/48 kHz stereo (x realtime) | **12 to 19** | 7 to 9 | 9 to 12 |
| HE-AAC v1 speed, 44.1/48 kHz stereo (x realtime) | 3.6 to 3.9 | 1.8 to 2.0 | **4.2 to 4.7** |
| MHz for real-time HE-AAC v1 | 60 | 120 | **51** |
| Flash | 78 KB | 159 KB | **57 KB** |
| RAM: heap + static + stack | **about 81 KB** | about 175 KB | about 174 KB |
| HE-AAC speed with state in PSRAM, vs internal RAM (S3) | **1.00x** | 0.92x | 0.78x |
| Real time on an ESP32-C6 (no FPU), LC / HE-AAC | **yes / yes** (10x / 2.4x) | yes / yes (5.5x / 1.7x) | **no / no** (0.87x / 0.23x) |
| Starts on a classic ESP32 (no PSRAM) | yes | yes | **no** (131 KB state block, 108 KB free); **yes** with split allocations ([PR #36](https://github.com/nschimme/faac/pull/36)): LC 15.8x, HE 5.5x |

* **FAAD3 vs FAAD2:** about 1.4x faster for LC and 2.4x faster for HE-AAC, 2.8x less flash and about 9x less
  stack (6.5 vs 56 KB), for about the same total RAM; output matches ffmpeg as closely as FAAD2's. On this
  evidence FAAD3 is the better FAAD.
* **FAAD3 vs libhelix:** libhelix decodes LC about 1.4x faster and needs about half the RAM; FAAD3 decodes
  HE-AAC about 1.15x faster, uses 28% less flash, and has a cleaner API. LC on a small part: libhelix. HE-AAC
  where flash is the constraint: FAAD3. libhelix is the only one comfortable on every chip tested.
* **Where FAAD3 struggles:** no FPU (C6) makes it slower than real time; its single 131 KB state block does not
  fit a classic ESP32 until the allocations are split ([PR #36](https://github.com/nschimme/faac/pull/36)). With
  its state in PSRAM on the S3, LC costs nothing and HE-AAC about 22% (still 3.8x real time).
* **Read with care:** FAAD3 is a work-in-progress branch (`bd73c46c`), built without PS and capped at 2
  channels, which flatters its footprint; it is float code and any `double` in it is software-emulated. FAAD2's
  figures include unused PS code. Gate subset, not the full corpus; C6 and classic ESP32 only smoke-tested.

**Classic ESP32 (no PSRAM, 257 KB free internal RAM, largest block 108 KB):** libhelix, FAAD2 and FAAD3 (split allocations) decode
LC and HE-AAC v1 in real time; FAAC encodes LC stereo at 4.2 to 6.3x and, with a reduced-memory libfaac (state 161 to 120 KB,
byte-identical output, [limits section](#classic-esp32-limits-no-psram)), HE-AAC v1 stereo at 3.6x. The limits are RAM, not CPU.

**FAAC encoder on the S3** (libfaac `3a8d8d5f`, one clip per scenario, 51 successful encodes):
* **Speed:** 44.1/48 kHz stereo LC encodes at 4.6 to 10x real time (about 40 MHz for 64 kbps, 52 MHz for
  320 kbps); HE-AAC v1 at 4.7 to 7x; 16/24 kHz mono LC at 25 to 38x.
* **Memory:** 70 to 103 KB of internal RAM for LC, 158 KB for HE-AAC v1 stereo; 44 KB of flash.
* **Limits, all traced to libfaac itself (details in the encoder section):** it offers no HE-AAC v1 below 32 kHz,
  so the 5 mono speech scenarios are LC only; LC at 8 kbps per channel overshoots (22 kbps for a 16 kbps
  target, a quality floor that its auto mode avoids by picking HE-AAC); HE-AAC v1 at 48 kHz cannot exceed the AAC
  core limit of 288 kbps stereo, so the 320 kbps scenario lands at 264. The C6 cannot encode stereo in real
  time and the classic ESP32 cannot run HE-AAC v1 stereo with libfaac `3a8d8d5f` (it can with the reduced-memory
  libfaac below). No quality metric was computed.

## Quick start

Everything lives in `scripts/esp32/` and runs from the repo root:

```
scripts/esp32/setup_idf.sh                                     # once: pinned ESP-IDF under scripts/esp32/.toolchain
scripts/esp32/fetch_faad3.sh [ref]                             # once: snapshot of FAAD3 (default origin/stack-tests)
python3 scripts/esp32/esp32_dec_bench.py build  --target esp32s3
python3 scripts/esp32/esp32_dec_bench.py run    --target esp32s3 --port /dev/serial/by-id/<board> --gate --verify 16
python3 scripts/esp32/esp32_dec_bench.py report --target esp32s3
```

<details>
<summary>Decoder tool flags</summary>

Flags: `--codec helix,faad2,faad3,faad3s` (`faad3s` is FAAD3 with split allocations, see PR #36; append `-psram`, e.g. `faad3-psram`, to allocate the decoder state from PSRAM on the S3), `--scenarios a,b`, `--gate` (fixed gate clips, 4 per scenario),
`--limit N`, `--loops N` (decode passes per clip, best is reported; default 3),
`--verify N` (return PCM and SNR-check about 1 clip in N), `--max-bytes`, `--encoder`
(row-key substring of the encoder whose bitstreams are used, default `faac`),
`--profiles lc,he`, `--sbr 0|1` (build with or without SBR). Results are saved after every
clip to `results/esp32_dec_<target>.json` and a rerun skips what is already there.
Reference list of the pure helpers is in `docs/scripts.md`; unit tests (no hardware) are
`tests/test_esp32_dec_bench.py`.

</details>

## Decoder results: ESP32-S3 @ 240 MHz

Gate set: 4 clips per scenario, 27 scenarios, LC and HE-AAC v1 bitstreams, best of 3 passes.
All 588 decodes (196 bitstreams x 3 decoders) completed without error.

| Codec | Profile | Bitstreams | Mean xRT | Median xRT | Min xRT | MHz for 1x RT (mean / worst) |
|---|---|---|---|---|---|---|
| libhelix | LC | 108 | 30.4 | 18.7 | 11.7 | 11.9 / 20.6 |
| libhelix | HE | 88 | 4.1 | 3.8 | 3.6 | 59.9 / 67.3 |
| FAAD2 | LC | 108 | 14.5 | 8.2 | 6.7 | 24.5 / 35.6 |
| FAAD2 | HE | 88 | 2.0 | 1.9 | 1.8 | 119.4 / 130.6 |
| FAAD3 | LC | 108 | 19.8 | 11.4 | 9.0 | 17.9 / 26.7 |
| FAAD3 | HE | 88 | 4.8 | 4.5 | 4.2 | 50.6 / 57.3 |

The LC means are pulled up by the 16 and 24 kHz mono speech scenarios (Helix about 100x and 67x);
on 44.1/48 kHz stereo music LC runs 12 to 19x (Helix), 9 to 12x (FAAD3) and 7 to 8.6x (FAAD2).

* **LC:** Helix is the fastest, about 1.4x faster than FAAD3 and 2x faster than FAAD2.
  FAAD3 is about 1.4x faster than FAAD2.
* **HE-AAC v1:** FAAD3 is the fastest (about 1.15x faster than Helix and 2.4x faster than FAAD2),
  so the ranking flips; it needs about 50 MHz for real-time HE-AAC, Helix about 60 MHz, FAAD2
  about 120 MHz. On stereo music HE-AAC costs 2.4 to 4x the LC time, since SBR doubles the output rate.
* The S3 has a single-precision FPU, which suits FAAD3's float arithmetic; the C6 has none (see the
  smoke runs), and any `double` in FAAD3 is done in software on all three chips.

<details>
<summary>By scenario (mean xRT, all 27 scenarios)</summary>

| Scenario | Helix LC | FAAD2 LC | FAAD3 LC | Helix HE | FAAD2 HE | FAAD3 HE |
|---|---|---|---|---|---|---|
| 16k_mono_20k | 99.4 | 47.3 | 63.4 | - | - | - |
| 16k_mono_24k | 97.9 | 47.1 | 63.1 | - | - | - |
| 16k_mono_voip_24k | 93.4 | 45.6 | 62.2 | - | - | - |
| 24k_mono_28k | 67.2 | 31.8 | 42.5 | - | - | - |
| 24k_mono_32k | 66.7 | 31.7 | 42.5 | - | - | - |
| 32k_stereo_16k | 28.5 | 12.2 | 16.2 | 5.3 | 2.6 | 6.2 |
| 32k_stereo_48k | 24.5 | 11.5 | 15.9 | 5.1 | 2.5 | 6.1 |
| 32k_stereo_64k | 22.9 | 11.2 | 15.5 | 5.1 | 2.5 | 6.1 |
| 32k_stereo_80k | 22.2 | 11.1 | 15.3 | 5.1 | 2.5 | 6.1 |
| 32k_stereo_96k | 21.5 | 11.0 | 15.1 | 5.0 | 2.5 | 6.0 |
| 44k1_stereo_64k | 18.7 | 8.6 | 11.9 | 3.9 | 2.0 | 4.7 |
| 44k1_stereo_128k | 16.3 | 8.1 | 11.2 | 3.9 | 1.9 | 4.7 |
| 44k1_stereo_160k | 15.5 | 8.0 | 11.0 | 3.8 | 1.9 | 4.6 |
| 44k1_stereo_192k | 14.8 | 7.8 | 10.8 | 3.8 | 1.9 | 4.6 |
| 44k1_stereo_256k | 13.7 | 7.6 | 10.3 | 3.7 | 1.9 | 4.5 |
| 48k_stereo_24k | 19.0 | 8.1 | 10.8 | 3.9 | 1.9 | 4.5 |
| 48k_stereo_32k | 18.9 | 8.1 | 11.0 | 3.9 | 1.9 | 4.5 |
| 48k_stereo_40k | 18.9 | 8.1 | 11.2 | 3.8 | 1.9 | 4.4 |
| 48k_stereo_48k | 18.7 | 8.1 | 11.2 | 3.8 | 1.9 | 4.5 |
| 48k_stereo_56k | 18.6 | 8.1 | 11.3 | 3.8 | 1.9 | 4.5 |
| 48k_stereo_64k | 17.9 | 8.0 | 11.1 | 3.8 | 1.9 | 4.4 |
| 48k_stereo_96k | 16.0 | 7.7 | 10.6 | 3.8 | 1.9 | 4.4 |
| 48k_stereo_128k | 15.3 | 7.5 | 10.5 | 3.7 | 1.9 | 4.4 |
| 48k_stereo_160k | 14.6 | 7.4 | 10.2 | 3.7 | 1.9 | 4.3 |
| 48k_stereo_192k | 14.0 | 7.3 | 10.1 | 3.6 | 1.9 | 4.3 |
| 48k_stereo_256k | 13.0 | 7.1 | 9.7 | 3.6 | 1.8 | 4.2 |
| 48k_stereo_320k | 12.1 | 6.8 | 9.3 | 3.6 | 1.8 | 4.2 |

</details>

<details>
<summary>Memory and footprint (heap, stack, flash on all three chips)</summary>

| Codec | Decoder heap (KB, max) | Main-task stack used (KB, max) | Flash (.text + .rodata + .data) | Static RAM |
|---|---|---|---|---|
| libhelix | 79 | 1.6 | 78,253 B (text 37,157, rodata 41,096) | 4 B |
| FAAD2 | 117 | 56.5 | 158,998 B (text 103,848, rodata 53,772) | 1,386 B |
| FAAD3 | 131 (caller-owned state block) | 6.5 | 56,719 B (text 44,961, rodata 11,758) | 36,044 B |

All three include SBR. FAAD2 also carries its unused PS code, FAAD3 was built without PS, and
FAAD3 is capped at 2 channels (`MAX_CHANNELS=2`), so its footprint is the smallest partly through
build choices. FAAD3 also has about 36 KB of static RAM (`.bss`), which the other two do not. The same libraries on the other chips (flash):

| Chip | libhelix | FAAD2 | FAAD3 |
|---|---|---|---|
| ESP32-S3 (Xtensa LX7) | 78,253 B | 158,998 B | 56,719 B |
| ESP32-C6 (RISC-V) | 82,024 B | 145,612 B | 62,986 B |
| ESP32 (Xtensa LX6) | 78,082 B | 142,896 B | 56,552 B |

</details>

<details>
<summary>Correctness check against ffmpeg (12 clips verified by all three decoders)</summary>

About 1 clip in 16 is returned as PCM and compared with ffmpeg's decode (the same clips for every
decoder). Per clip, SNR in dB, Helix / FAAD2 / FAAD3:

| Profile | Scenario | Clip | Helix | FAAD2 | FAAD3 |
|---|---|---|---|---|---|
| HE | 32k_stereo_16k | 21-classic | 16.1 | 16.1 | 16.2 |
| HE | 32k_stereo_16k | velvet | 11.8 | 11.7 | 11.7 |
| HE | 44k1_stereo_128k | velvet | 29.9 | 29.8 | 30.1 |
| HE | 48k_stereo_24k | fms | 9.9 | 10.0 | 9.8 |
| HE | 48k_stereo_32k | sandman | 14.1 | 14.1 | 14.0 |
| HE | 48k_stereo_40k | fms | 21.9 | 21.8 | 21.9 |
| HE | 48k_stereo_56k | 21-classic | 35.2 | 35.2 | 35.2 |
| HE | 48k_stereo_128k | 21-classic | 54.7 | 54.7 | 54.7 |
| HE | 48k_stereo_192k | 21-classic | 81.5 | 75.8 | 104.4 |
| LC | 44k1_stereo_64k | sandman | 19.3 | 19.3 | 19.3 |
| LC | 44k1_stereo_128k | sandman | 30.0 | 30.1 | 30.0 |
| LC | 48k_stereo_128k | 21-classic | 50.6 | 50.5 | 50.6 |

The three decoders agree with each other to about 0.2 dB on every clip except one, where FAAD3
(float) comes closest to ffmpeg. SNR against ffmpeg falls with bitrate (50 dB at 128 kbps, 10 to 16 dB
at 24 to 32 kbps), so the low values are a property of the stream and not of one decoder; the likely
cause is noise substitution that decoders render differently, but that has not been checked. This is
a sanity check that the on-device output is sound, not a conformance result: the host harness's
60 dB gate is the stricter test. One more caveat: the 16 kbps, 32 kHz stereo LC streams give about -2 dB
for Helix and FAAD2 (and for the x86 build of Helix); FAAD3 was not among their verified clips, and the
cause is uninvestigated.

</details>

<details>
<summary>Why FAAD3's state block is large</summary>

`faad_get_state_size()` returns `sizeof(struct faad_decoder)`: one block holding every buffer for the
worst case at once. On this build (2 channels, SBR on, PS off) it is about 125 KB of struct (measured on
x86-64; 131 to 134 KB on the S3 with alignment), of which:

| Part | Size | What it is |
|---|---|---|
| Frame scratch (a union) | 50 KB | working memory for one frame; sized for the SBR buffers (`x_low`, `y`, `x`) |
| SBR channel state | 34 KB | QMF history and envelope state, 2 channels |
| `spec`, `overlap`, `prev_spec` | 24 KB | three float spectra of 1024 values per channel |
| `pcm` | 16 KB | float core output, then SBR output in place (2 channels x 2048 samples) |

Building with PS on grows it to 147 KB. FAAD2 allocates its buffers separately, as the channels and tools of a stream need them, and
libhelix's largest allocation is well under 80 KB, so neither needs one big contiguous block; FAAD3
trades that for a single caller-owned block and no allocator at all. A smaller state
would need a lower `MAX_CHANNELS` (mono only) or a scratch region the caller supplies. The contiguity problem,
though, is removed without making the state smaller by [nschimme/faac#36](https://github.com/nschimme/faac/pull/36),
which splits the `faad_decoder_open()` state into ten blocks (largest 33,760 B, total 125,763 B, output bit-exact).

</details>

<details>
<summary>State in PSRAM instead of internal RAM (S3)</summary>

Same bitstreams, decoder state (and, for FAAD2, every `faad_malloc`) allocated from 80 MHz octal PSRAM
instead of internal RAM; the clip and the PCM scratch buffer stay where they were. Representative subset:
6 scenarios (two mono speech, four stereo music), 40 bitstreams per codec, mean xRT over the same clips.

| Codec | Profile | Bitstreams | xRT internal | xRT PSRAM | PSRAM / internal | Internal heap used (KB, internal vs PSRAM) | CRC identical |
|---|---|---|---|---|---|---|---|
| libhelix | LC | 24 | 39.5 | 39.1 | 0.99x | 79 vs 50 | 24/24 |
| libhelix | HE | 16 | 4.1 | 4.1 | 1.00x | 79 vs 50 | 16/16 |
| FAAD2 | LC | 24 | 18.9 | 18.9 | 1.00x | 117 vs 0 | 24/24 |
| FAAD2 | HE | 16 | 2.1 | 1.9 | 0.92x | 117 vs 0 | 16/16 |
| FAAD3 | LC | 24 | 25.6 | 25.6 | 1.00x | 131 vs 8 | 24/24 |
| FAAD3 | HE | 16 | 4.9 | 3.8 | 0.78x | 131 vs 8 | 16/16 |

* **LC costs nothing** (1.00x for all three) and the decoded PCM is bit-identical (CRC equal on every clip).
* **HE-AAC costs more**, because SBR walks much more memory per frame: libhelix none, FAAD2 about 8%, FAAD3
  about 22% (still 3.8x real time on stereo music, or about 63 MHz for 1x).
* So a build short of internal RAM can put a decoder's state in PSRAM; for LC it is free, and for HE-AAC
  FAAD3 keeps its lead over FAAD2 and only falls just behind libhelix.
* libhelix keeps about 50 KB in internal RAM in this mode, so some of its allocations do not go through the
  switch; that was not chased.

</details>

<details>
<summary>FAAD3 with split allocations on the S3 (speed cost: none)</summary>

[nschimme/faac#36](https://github.com/nschimme/faac/pull/36) (`faad3s`) against the single-block build (`faad3`),
same 40 bitstreams (6 scenarios, two mono speech and four stereo music), best of 3, ESP32-S3 at 240 MHz:

| Profile | Bitstreams | xRT single block | xRT split | Cycles split / single | PCM CRC identical | Internal heap used |
|---|---|---|---|---|---|---|
| LC | 24 | 25.6 | 25.7 | 0.998 (0.997 to 1.000) | 24/24 | 130 KB vs 130 KB |
| HE-AAC v1 | 16 | 4.9 | 4.9 | 0.995 (0.993 to 0.997) | 16/16 | 130 KB vs 130 KB |

Splitting the state into ten allocations costs no measurable speed and changes no output, so it is what lets FAAD3 run on a
classic ESP32 (see the smoke runs).

</details>

<details>
<summary>Smoke runs: ESP32-C6 and classic ESP32</summary>

Stability check, not a benchmark. Each codec was built, flashed, booted and run on 4 bitstreams
(2 clips, LC and HE). The speeds come from each chip's own clips, so they are indicative only; the
ESP32 ran libhelix on `48k_stereo_24k` and FAAD2 and FAAD3 on `32k_stereo_16k`. The ESP32 WROOM has
no PSRAM and little internal RAM, so it only takes the smallest clips (about 20 to 43 KB).

| Chip | MHz | Codec | LC mean xRT | HE mean xRT | Clips (scenario) | Result |
|---|---|---|---|---|---|---|
| ESP32-C6 | 160 | libhelix | 10.1 | 2.4 | 48k_stereo_48k | ok |
| ESP32-C6 | 160 | FAAD2 | 5.5 | 1.7 | 48k_stereo_48k | ok |
| ESP32-C6 | 160 | FAAD3 | **0.87** | **0.23** | 48k_stereo_48k | ok, but **below real time** |
| ESP32 | 240 | libhelix | 13.1 | 3.3 | 48k_stereo_24k | ok |
| ESP32 | 240 | FAAD2 | 9.4 | 2.9 | 32k_stereo_16k | ok |
| ESP32 | 240 | FAAD3 | - | - | 32k_stereo_16k | **does not start** (single 131 KB block, see below) |
| ESP32 | 240 | FAAD3, split allocations | 15.8 | 5.5 | 32k_stereo_16k | ok; PCM CRC identical to the S3's single-block run (4 of 4) |

* **Fixed-point decoders are bit-exact across chips:** the PCM CRC32 of every libhelix and FAAD2 decode
  was identical to the S3's (8 of 8 on each chip), across Xtensa LX6, Xtensa LX7 and RISC-V.
* **FAAD3 on the C6 is correct but too slow.** The C6 has no FPU, so FAAD3's float code runs in
  software: LC decodes at 0.85 to 0.88x real time and HE-AAC at 0.23x (one pass; three passes exceeded the
  host's wait). Output is sound, 102 dB SNR against ffmpeg on a short clip, but not bit-identical to the S3's
  (different float rounding, 0 of 4 CRCs equal, same sample counts). The C6 can run libhelix and FAAD2 in real time
  but not FAAD3.
* **FAAD3 does not start on the classic ESP32.** `faad_decoder_init` needs one contiguous block of about
  131 KB, and the largest contiguous internal block on an ESP32 without PSRAM is 108 KB (110,592 B, with
  251 KB free in total, because its DRAM is split into regions). FAAD2 and libhelix do not hit this: FAAD2
  allocates its buffers separately as it needs them, and libhelix's largest allocation is smaller. FAAD3
  needs either a smaller state or a part with PSRAM.
* **FAAD3 with split allocations runs on the classic ESP32.** [nschimme/faac#36](https://github.com/nschimme/faac/pull/36)
  makes `faad_decoder_open()` allocate the state as ten blocks, the largest 33,760 B, instead of one 125 KB block
  (`faad3s` in the benchmark, built from the `faad3-split` branch, `CODEC_FAAD3_OPEN=1`). On the ESP32 it decodes LC at
  15 to 16.6x and HE-AAC at 5.4 to 5.6x real time, faster than libhelix and FAAD2 on the same chip, with an internal-RAM
  footprint of about 132 to 144 KB, and its PCM is bit-identical to the S3's single-block output on all four streams (float
  results are identical here because both chips have a hardware FPU; the FPU-less C6 differs). On the S3 the split build is
  as fast as the single block (see the section above).

</details>

<details>
<summary>ESP32-S3 vs classic ESP32 on identical clips (both 240 MHz)</summary>

The smoke table above is not a chip comparison: the ESP32 ran short, low-rate 32 kHz clips and the S3 numbers in
the results tables are 44.1/48 kHz music, so the ESP32 looks faster. Cycles for the same bitstream and the same
decoder (best of 3, S3 divided by ESP32; below 1.0 means the S3 needs fewer cycles):

| Decoder | Profile | Clips | S3 / ESP32 cycles |
|---|---|---|---|
| libhelix | LC | 2 | 0.64 to 0.77 |
| libhelix | HE | 2 | 0.84 |
| FAAD2 | LC | 2 | 0.72 to 0.83 |
| FAAD2 | HE | 2 | **1.12 to 1.13** |
| FAAD3 (split) | LC | 2 | 0.97 to 0.98 |
| FAAD3 (split) | HE | 2 | 0.89 |

The S3 is the same speed or faster in 10 of 12 comparisons, as expected for the same clock and a newer core. FAAD2 HE-AAC
is the exception: the S3 needs about 12% more cycles for it, and the cause has not been found. Differences between the
two builds that could matter are the S3's PSRAM-resident input clip, its larger data cache and its cache line size; none
was isolated.

</details>

## FAAC encoder

An on-device benchmark of the FAAC *encoder* (libfaac from `../faac`, float, `FAAC_OBJ_LOW` and
`FAAC_OBJ_HE_AAC_V1`). It is separate from the decoder tool: own firmware
(`scripts/esp32/firmware_enc/`, see its README for the wire protocol), own CLI
(`scripts/esp32/esp32_enc_bench.py`), own results (`results/esp32_enc_<target>.json`), tests in
`tests/test_esp32_enc_bench.py`. As for the decoders, the ESP32-S3 is the benchmark target; the ESP32-C6
and classic ESP32 only got a smoke run (below).

```
python3 scripts/esp32/esp32_enc_bench.py build  --target esp32s3 [--faac-dir ../faac] [--sbr-decimation N]
python3 scripts/esp32/esp32_enc_bench.py run    --target esp32s3 --port /dev/serial/by-id/<board> --gate
python3 scripts/esp32/esp32_enc_bench.py report --target esp32s3
```

<details>
<summary>Encoder tool flags</summary>

`run` needs pyserial (the repo `.venv` has it). Flags: `--scenarios`, `--gate`, `--clips N` per
scenario, `--seconds` (default 10; trimmed so the PCM fits the chip), `--start` (offset into the clip),
`--profiles lc,he,auto`, `--loops` (passes per clip, best is reported), `--verify 0|1` (return the
ADTS bitstream, check it and decode it with ffmpeg; default 1), `--keep DIR` (save the bitstreams),
`--no-flash`. Results are saved after every clip and a rerun skips what is already there.

</details>

<details>
<summary>How the encoder is measured</summary>

How it measures: the host cuts `--seconds` of each corpus clip with ffmpeg (corpus rate and channels
from `config.CORPORA`, target rate from the scenario; the scenario's total kbps is divided by the
channel count because libfaac's `bit_rate` is per channel) and sends it as s16le PCM. The device encodes
it `--loops` times and times only `faac_encoder_encode` calls (including the end-of-stream flush) with the
CPU cycle counter; open/close and the copy of each input frame into internal RAM are untimed. The
board is rebooted before every clip so the heap low-water mark is clean. The report gives xRT, MHz
for 1x RT, actual vs target kbps (over the duration the stream covers, priming and flush frames
included), encoder heap, stack, and the libfaac archive's flash from the IDF map. The returned
ADTS is checked against the device's CRC32, size and frame count and decoded with ffmpeg (SNR against
the source is only a sanity figure).

Build notes: sources are `libfaac/*.c` without `quantize_sse.c`; `FAAC_MAX_CHANNELS` is 2 (state
scales with it, 5.1 scenarios are skipped); a stand-in `config.h` replaces meson's. The library uses
`double` in a few places (filterbank, frame, quantizer), which is software-emulated on all three
chips.

</details>

### Results: ESP32-S3 @ 240 MHz

`run --target esp32s3 --gate --loops 3`, libfaac `3a8d8d5f`, ESP-IDF v5.5.5, default 10 s clips, every
bitstream returned and checked. The gate list has **one clip per scenario** for the encoder (28 scenarios), each encoded as
LC and as HE-AAC v1: 56 requested, **51 ran, 5 not applicable** (HE-AAC v1 on 16/24 kHz mono, which libfaac does not offer, see below). The run took about 21 minutes
(board reboot and flash per clip included). Passes are deterministic: mean and best of 3 differ by 0.1% or less.
Results: `results/esp32_enc_esp32s3.json`.

By profile:

| Profile | Encodes | Mean xRT | Min xRT | MHz for 1x RT (mean / worst) | Mean actual/target kbps | Encoder heap | Stack used | ffmpeg decodes |
|---|---|---|---|---|---|---|---|---|
| LC | 28 | 11.8 | 4.6 | 31 / 52 | 1.03x | 70 KB mono, 103 KB stereo | 8.9 KB | 28/28 |
| HE-AAC v1 | 23 (+5 not applicable) | 6.3 | 4.7 | 40 / 51 | 1.00x | 158 KB stereo | 13.7 KB | 23/23 |

Reading: a stereo 44.1/48 kHz encode needs about 40 MHz (64 kbps) to 52 MHz (320 kbps) per real-time stream, so
the S3 at 240 MHz encodes 4.6 to 10x faster than real time on one core; a 16 kHz mono voice stream needs 6 to 7 MHz.
Cost grows with bitrate (more nonzero coefficients to quantize and Huffman-code), and HE-AAC v1 costs
more than LC at low and mid rates (about 1.1 to 1.35x at 24 to 128 kbps, 48 kHz) but is *not* more expensive at 192 kbps and
above, where the two are within a few percent (HE is even marginally faster at 256 and 320 kbps; there libfaac
clamps the HE core, see accuracy). These xRT figures are for the encode call only; they are about the same magnitude as
the S3 decoder results above (FAAD3 HE-AAC decode 4.2 to 4.7x, libhelix LC 12 to 19x); HE-AAC v1 encoding is a little faster than FAAD3 decoding it.

<details>
<summary>By family (xRT and MHz per sample-rate family)</summary>

By family (min to max over the family's scenarios, i.e. over bitrate; xRT falls as the bitrate rises):

| Family | Profile | Scenarios | xRT | MHz for 1x RT |
|---|---|---|---|---|
| 16 kHz mono | LC | 3 | 36.8 to 38.2 | 6 to 7 |
| 24 kHz mono | LC | 2 | 24.9 to 25.3 | 9 to 10 |
| 32 kHz stereo | LC | 5 | 7.5 to 13.6 | 18 to 32 |
| 32 kHz stereo | HE-AAC v1 | 5 | 7.6 to 10.1 | 24 to 32 |
| 44.1 kHz stereo | LC | 5 | 4.9 to 7.4 | 33 to 49 |
| 44.1 kHz stereo | HE-AAC v1 | 5 | 4.9 to 6.2 | 39 to 49 |
| 48 kHz stereo | LC | 13 | 4.6 to 10.4 | 23 to 52 |
| 48 kHz stereo | HE-AAC v1 | 13 | 4.7 to 7.1 | 34 to 51 |


</details>

<details>
<summary>By scenario (all 28 scenarios)</summary>

By scenario (xRT / MHz for 1x RT, actual kbps; one clip each, 10 s sandman for stereo; mono clips are only
8.6 s long in the corpus):

| Scenario | Source | LC xRT / MHz | LC kbps | HE xRT / MHz | HE kbps | Target |
|---|---|---|---|---|---|---|
| 16k_mono_20k | R_01_CLIP_MK 8.6 s | 38.2 / 6 | 21.0 | failed (open) | - | 20 |
| 16k_mono_24k | R_01_CLIP_MK 8.6 s | 36.8 / 7 | 22.7 | failed (open) | - | 24 |
| 16k_mono_voip_24k | C_01_CHOP_FA 8.6 s | 37.5 / 6 | 23.9 | failed (open) | - | 24 |
| 24k_mono_28k | R_01_CLIP_MK 8.6 s | 25.3 / 9 | 27.9 | failed (open) | - | 28 |
| 24k_mono_32k | R_01_CLIP_MK 8.6 s | 24.9 / 10 | 29.4 | failed (open) | - | 32 |
| 32k_stereo_16k | sandman 10 s | 13.6 / 18 | 17.7 | 10.1 / 24 | 17.2 | 16 |
| 32k_stereo_48k | sandman 10 s | 9.7 / 25 | 49.2 | 8.4 / 29 | 48.3 | 48 |
| 32k_stereo_64k | sandman 10 s | 8.7 / 28 | 64.9 | 8.0 / 30 | 63.6 | 64 |
| 32k_stereo_80k | sandman 10 s | 8.0 / 30 | 80.7 | 7.7 / 31 | 79.2 | 80 |
| 32k_stereo_96k | sandman 10 s | 7.5 / 32 | 96.5 | 7.6 / 32 | 94.8 | 96 |
| 44k1_stereo_64k | sandman 10 s | 7.4 / 33 | 65.6 | 6.2 / 39 | 64.2 | 64 |
| 44k1_stereo_128k | sandman 10 s | 5.9 / 41 | 128.8 | 5.5 / 43 | 126.9 | 128 |
| 44k1_stereo_160k | sandman 10 s | 5.5 / 43 | 160.4 | 5.5 / 44 | 158.3 | 160 |
| 44k1_stereo_192k | sandman 10 s | 5.3 / 46 | 192.0 | 5.5 / 44 | 189.6 | 192 |
| 44k1_stereo_256k | sandman 10 s | 4.9 / 49 | 255.2 | 4.9 / 49 | 244.6 | 256 |
| 48k_stereo_16k | sandman 10 s | 10.4 / 23 | 22.0 | 7.1 / 34 | 17.5 | 16 |
| 48k_stereo_24k | sandman 10 s | 8.9 / 27 | 26.4 | 6.8 / 35 | 25.0 | 24 |
| 48k_stereo_32k | sandman 10 s | 8.5 / 28 | 34.2 | 6.4 / 37 | 32.9 | 32 |
| 48k_stereo_40k | sandman 10 s | 8.0 / 30 | 42.1 | 6.2 / 39 | 40.8 | 40 |
| 48k_stereo_48k | sandman 10 s | 7.7 / 31 | 50.1 | 6.0 / 40 | 48.7 | 48 |
| 48k_stereo_56k | sandman 10 s | 7.6 / 32 | 58.0 | 5.9 / 41 | 56.4 | 56 |
| 48k_stereo_64k | sandman 10 s | 7.0 / 34 | 65.8 | 5.8 / 41 | 64.2 | 64 |
| 48k_stereo_96k | sandman 10 s | 6.0 / 40 | 97.5 | 5.4 / 44 | 95.6 | 96 |
| 48k_stereo_128k | sandman 10 s | 5.7 / 42 | 129.1 | 5.2 / 46 | 127.0 | 128 |
| 48k_stereo_160k | sandman 10 s | 5.3 / 45 | 160.7 | 5.1 / 47 | 158.4 | 160 |
| 48k_stereo_192k | sandman 10 s | 5.1 / 47 | 192.4 | 5.1 / 47 | 189.8 | 192 |
| 48k_stereo_256k | sandman 10 s | 4.7 / 51 | 255.6 | 5.0 / 48 | 252.6 | 256 |
| 48k_stereo_320k | sandman 10 s | 4.6 / 52 | 318.8 | 4.7 / 51 | 264.1 | 320 |

</details>

<details>
<summary>Memory and footprint</summary>

* **Encoder state** (internal heap taken by `faac_encoder_open`, `FAAC_MAX_CHANNELS` 2): 70 KB LC mono, 103 KB LC stereo,
  158 KB HE-AAC v1 stereo. It is the same at every bitrate and sample rate. Static RAM of the library is 20.5 KB `.bss` +
  1 KB `.data`. The PCM input stays in PSRAM on the S3, so the state is the whole internal-RAM cost.
* **Stack:** 8.8 to 8.9 KB (LC) and 13.7 KB (HE-AAC v1) of the 32 KB main task stack (the high-water mark of the firmware's own task, so
  a real task needs about that plus its own frames).
* **Flash:** 43.9 KB for the libfaac archive (`.text` 33.7 KB + `.rodata` 9.1 KB), including HE-AAC v1; the C6 build is
  51.5 KB and the classic ESP32 43.6 KB.
* **Open cost:** `faac_encoder_open` is 31 to 48 k cycles for LC (0.2 ms) and 0.64 M cycles for HE-AAC v1 (2.7 ms), after the
  one-time table setup, so a short-lived encoder instance is cheap.
* Compared with the decoders, encoding LC stereo takes 103 KB of RAM, more than libhelix (about 81 KB total) and
  less than FAAD2 or FAAD3 (about 175 KB), and HE-AAC v1 stereo takes 158 KB; the flash is in the FAAD3 range (57 KB) and below FAAD2.

</details>

<details>
<summary>Accuracy against the target bitrate</summary>

The target is the scenario's total kbps; libfaac gets it per channel. Actual kbps is the output size over the duration
the stream covers (priming and flush frames included), and over the 51 runs the mean ratio is 1.03x (LC) and 1.00x (HE).

* LC: within 0 to 5% over the target from 24 kbps up (0.92 to 1.05 on mono, 1.00 to 1.03 above 48 kbps
  stereo), **but 11% over at 32k_stereo_16k and 37% over at 48k_stereo_16k** (22.0 vs 16 kbps) and 10% over at 48k_stereo_24k. The 16 kbps
  stereo scenarios (8 kbps per channel) sit on a libfaac quality floor: the resolved bit rate is 8000 per channel, the bandwidth falls to 9.75 kHz, and the
  stream stays well above it. The SNR for that row is -0.5 dB, which with the other low-rate rows (5 to 8 dB) says the content
  is not really coded at that rate; SNR is only a sanity figure here.
* LC mono comes out slightly under on the 24 and 32 kbps rows (22.7 vs 24 and 29.4 vs 32); the speech clips have quiet passages. With
  the full-length clips (8.6 s, not the 2 to 3 s of the smoke run) this is 5 to 8%, not the large undershoot seen on the short clips.
* HE-AAC v1: 0.99 to 1.08 up to 192 kbps, 0.96 at 44.1 kHz/256 kbps, and **0.83 at 48k_stereo_320k (264 vs 320 kbps)**:
  the resolved per-channel rate reported by the device is 144 kbps there (160 requested), a clamp inside libfaac, so HE-AAC v1 cannot be pushed to 320 kbps. Not a tool problem; the rate it asks for is clamped inside libfaac.

</details>

<details>
<summary>Correctness checks and root causes of the limits</summary>

* All 51 returned ADTS streams decode with ffmpeg at the expected profile (LC or HE-AAC v1), rate and channels, and the device CRC32,
  byte count and frame count matched the returned stream in every case (CRC, size and frame checks 51/51).
* The earlier smoke run additionally compared the device output with the same configuration built with host gcc:
  byte-identical on the C6 (LC mono 16 kHz, LC/HE stereo 48 kHz).
* SNR against the source (5 to 38 dB LC, 7 to 28 dB HE) is not a quality metric: the decode is offset by the codec delay
  and the HE-AAC output is resampled; it only shows that the stream is not garbage. No MOS was computed.

**Root causes of the "failures" and outliers** (each checked against the libfaac source; the overshoot also reproduced
with the host `faac` built from the same commit):

* **HE-AAC v1 rejected on 16/24 kHz mono (5 rows).** `libfaac/frame.c` defines `HE_MIN_SAMPLE_RATE` 32000 and `open()` returns
  an error for an explicit HE-AAC v1 request below it ("Fs/2 < 16 kHz, core too narrow for SBR"); the auto mode never picks HE
  there either. The tool asked for HE on every scenario, so the fault was the request: it now skips HE below 32 kHz and
  reports those rows as not applicable. Not a device or library bug.
* **HE-AAC v1 at 320 kbps lands at 264 kbps (48 kHz).** After the HE resolution halves the core rate to 24 kHz, `frame.c` clamps the
  bit rate to `MaxBitrate(core rate)`, the AAC limit of 6144 bits per frame and channel: 144 kbps per channel, 288 kbps stereo. The
  320 kbps target is not reachable in HE-AAC v1 at 48 kHz (LC reaches it). The clamp is silent at `open()`; the encoder info
  reports the resolved rate, which the tool now flags as "clamped".
* **LC overshoots at 8 kbps per channel (22.0 kbps for 16; +10% at 24 kbps, 48 kHz).** Reproduced on the host with `faac -b 16
  --object-type lc` (22.0 kbps, identical to the device), so it is not a device effect. It is libfaac's low-rate LC quality floor
  (`MINQUAL`, already known from earlier ABR analysis). With `--object-type auto` the same clip comes out at 17.7 kbps because
  auto resolves to HE-AAC at 8 kbps per channel. Forcing LC there is outside the encoder's tuned range.
* **Mono speech 5 to 8% under target** on full clips (larger on 2 to 3 s clips): rate-control convergence and quiet passages, not a fault.
* **Classic ESP32 could not run HE-AAC v1 stereo with libfaac `3a8d8d5f`:** the 161 KB encoder state left no room for the
  PCM and output buffers in its internal RAM. A reduced-memory libfaac (120 KB, byte-identical output, see
  "Classic ESP32 limits") runs it.

A libfaac code change is not indicated by any of these; the possible improvements are design choices (reporting a clamped rate
at `open()`, a lower LC floor) rather than bug fixes.

</details>

<details>
<summary>Encoder caveats</summary>

* One clip per scenario, and every stereo row uses the same music clip (`sandman`, first 10 s), so content effects (e.g. the
  16 kbps overshoot) are not separated from rate effects; the mono rows use two speech clips of 8.6 s. Cost varies with content, so
  treat the xRT as a representative point, not a worst case. Run the full corpus (without `--gate`) for ranges.
* Single core, the encode call only, with the library's `double` code software-emulated on all three chips; changing that or the
  compiler flags would move these numbers. The build uses the ESP-IDF defaults with no tuning, and `sbr-decimation` 1.
* `FAAC_MAX_CHANNELS` is 2, so the 5.1 scenarios are skipped; larger channel counts need more RAM than the S3's internal heap offers.
* The ESP32-C6 (no FPU) cannot encode stereo in real time (0.2 to 0.6x in the smoke run) and the classic ESP32 could not run HE-AAC v1 stereo with this libfaac (no room
  for the state); see the smoke results.
* No quality measurement was made; the bitrate accuracy is the only rate/quality statement.
* Tested with libfaac `3a8d8d5f` (`git rev-parse --short HEAD` of `../faac`, master), ESP-IDF v5.5.5, GCC 14.2.0, ffmpeg 7.1.5.

</details>

<details>
<summary>Smoke results: ESP32-C6 and classic ESP32 (not a benchmark)</summary>

Short clips (0.3 to 3 s, the longest that fits the internal RAM of these chips), starting 2 s into the
clip, best of 3 passes, bitstreams verified. **Every bitstream decoded with ffmpeg, the device CRC32,
size and frame count matched the returned stream, and the device output was byte-identical to the
same configuration built with host gcc** (checked on the C6 for LC mono 16 kHz and LC/HE stereo 48 kHz).

| Chip | Profile | Scenario | xRT | MHz for 1x RT | kbps (target) |
|---|---|---|---|---|---|
| ESP32-C6 @ 160 | LC | 16k_mono_24k | 4.4 | 36 | 12.0 (24) |
| ESP32-C6 @ 160 | LC | 48k_stereo_64k | 0.45 | 353 | 66.1 (64) |
| ESP32-C6 @ 160 | HE-AAC v1 | 48k_stereo_64k | 0.23 | 705 | 69.8 (64) |
| ESP32-C6 @ 160 | LC | 44k1_stereo_128k | 0.41 | 389 | 126.9 (128) |
| ESP32 @ 240 | LC | 16k_mono_24k | 33.8 | 7 | 17.3 (24) |
| ESP32 @ 240 | LC | 48k_stereo_64k | 5.7 | 42 | 66.1 (64) |
| ESP32 @ 240 | LC | 44k1_stereo_128k | 4.8 | 50 | 125.8 (128) |

* The C6 has no FPU, so a stereo encode is below real time there (0.2 to 0.6x); the classic ESP32 does
  stereo LC at about 5x. Mono speech intros are quiet, so their short clips encode at well below the
  target rate and look faster than a 10 s run will.
* Memory: encoder state is 71 KB (LC mono), 105 KB (LC stereo) and 161 KB (HE-AAC stereo) of internal
  RAM; stack use is 8.6 KB (LC) to 13.8 KB (HE) of the 32 KB main task stack; 20 KB of static RAM.
  With this libfaac the classic ESP32 had no room for the HE-AAC stereo state (about 175 KB with its buffers) and
  kept only about 64 KB of PCM (0.33 s of 48 kHz stereo) next to LC stereo, so it could not run HE-AAC. The S3 keeps
  the PCM in PSRAM. See "Classic ESP32 limits" for the reduced-memory libfaac, which does run it.
* Flash (libfaac archive, `.text + .rodata`): 43.9 KB on the S3 build, 51.5 KB on the C6, 43.6 KB on ESP32.
* HE-AAC mono (`16k_mono_*`, `24k_mono_*`) is rejected by libfaac at open ("encoder open failed", status 3),
  so those rows are recorded as failures rather than skipped.

</details>

## Classic ESP32 limits (no PSRAM)

What the original ESP32 (ESP32-D0WDQ6 rev v1.0, 240 MHz, no PSRAM) can run, found by probing the real board. The binding
constraint is internal RAM: 257,224 B are free after boot, but DRAM is split into regions, so the largest contiguous
block is only 110,592 B (108 KB). Wi-Fi, Bluetooth and the rest of an application come out of the same pool and were
not measured.

* **Decoders:** all three run LC and HE-AAC v1 at up to 48 kHz stereo in real time. libhelix leaves the most room (79 KB heap,
  1.6 KB stack); FAAD2 needs 117 KB of heap and 57 KB of stack for HE-AAC; FAAD3 needs the split allocations
  ([PR #36](https://github.com/nschimme/faac/pull/36)) because its single 131 KB state block does not fit.
* **Encoder, LC:** mono 58 KB and stereo 92 KB of state with the reduced-memory libfaac (71 and 105 KB with `3a8d8d5f`); 4.2 to 6.3x
  real time for stereo, so the CPU is not the limit at any bitrate up to the format ceiling.
* **Encoder, HE-AAC v1:** needs 32 kHz or more; stereo state 120 KB with the reduced-memory libfaac (161 KB with `3a8d8d5f`, which
  left no room for any clip); 3.6x real time at 44.1/48 kHz and 5.3x at 32 kHz.
* **Whole-clip benchmarking is the tighter limit:** the firmware holds the PCM in one contiguous block, so a clip is at most
  about 3.4 s of 16 kHz mono, 0.5 s of 48 kHz stereo LC or 0.36 s of 48 kHz stereo HE-AAC. A streaming encoder does not need that.

<details>
<summary>Encoder limits: state size, longest clip, speed (classic ESP32)</summary>

`faac_encoder_open()` state (host allocation profile, 48 kHz, `FAAC_MAX_CHANNELS` 2; the device reads 71, 103 and 158 KB for the
first build, within 2%), before and after a memory reduction of libfaac written by Codex ([nschimme/faac#37](https://github.com/nschimme/faac/pull/37), branch `faac-he-memory`,
commit `5a5f6a10`, based on `3a8d8d5f`). The reduction narrows or shares per-channel Huffman, codebook and retry buffers, the SBR
delay ring and the resampler copies; the encoded output is byte-identical on 1,600 matrix files (LC and HE-AAC v1, mono, stereo and
5.1, ABR and VBR, TNS/PNS on and off) and the host encode time moved by -0.6% to +1.3%.

| Configuration | State before | State after | Largest single allocation (before / after) |
|---|---|---|---|
| LC mono | 71,488 B | 58,048 B | not measured |
| LC stereo | 105,384 B | 91,560 B | 29,400 / 16,344 B |
| HE-AAC v1 mono | 119,400 B | 78,252 B | not measured |
| HE-AAC v1 stereo | 161,488 B | 119,956 B | 29,400 / 16,384 B |

Longest clip the board accepts, found by bisection (encode only, bitstream not returned; one contiguous PCM buffer, 0.01 s
resolution):

| Configuration | libfaac `3a8d8d5f` | Reduced-memory libfaac | PCM held |
|---|---|---|---|
| LC mono, 16 kHz | 3.44 s | 3.44 s | 110 KB (the largest block) |
| LC mono, 24 kHz | 2.29 s | 2.29 s | 110 KB |
| LC stereo, 32 kHz | 0.67 s | 0.77 s | 86 to 98 KB |
| LC stereo, 44.1 kHz | 0.49 s | 0.55 s | 86 to 97 KB |
| LC stereo, 48 kHz (64 to 320 kbps, same at every rate) | 0.42 s | 0.52 s | 81 to 99 KB |
| HE-AAC v1 stereo, 32 kHz | 0.21 s | 0.55 s | 26 to 70 KB |
| HE-AAC v1 stereo, 44.1 kHz | 0.14 s | 0.39 s | 25 to 69 KB |
| HE-AAC v1 stereo, 48 kHz (64 and 256 kbps) | 0.14 s | 0.36 s | 28 to 69 KB |

Returning the bitstream for checking needs a second buffer and shortens these (HE-AAC v1 stereo at 44.1/48 kHz then fits about 0.2 s).
The mono limit is the largest contiguous block itself; the stereo ones are lower because the encoder state sits in the same region.

Speed (reduced-memory libfaac, best of 3, 0.2 to 0.3 s clips, so fixed per-encode costs weigh more than in a 10 s run):

| Configuration | xRT | MHz for 1x real time |
|---|---|---|
| LC stereo, 32 kHz, 64 kbps | 6.3 | 38 |
| LC stereo, 44.1 kHz, 128 kbps | 4.5 | 53 |
| LC stereo, 48 kHz, 64 / 128 kbps | 5.1 / 4.2 | 47 / 58 |
| HE-AAC v1 stereo, 32 kHz, 64 kbps | 5.3 | 45 |
| HE-AAC v1 stereo, 44.1 / 48 kHz | 3.6 to 3.8 | 64 to 66 |

Format limits that apply on every chip: HE-AAC v1 needs 32 kHz or more (libfaac refuses it below), and its bitrate is capped by the AAC
core limit of 6144 bits per frame and channel at the half-rate core (144 kbps per channel, 288 kbps stereo at 48 kHz), so 320 kbps
stereo is reachable only in LC. This build handles 1 or 2 channels; the 5.1 scenarios do not fit RAM.

</details>

<details>
<summary>Decoder limits: state, stack, largest resident stream, speed (classic ESP32)</summary>

Each decoder was flashed and probed on the board with a real 48 kHz stereo stream (LC and HE-AAC v1), cut to growing lengths. The
decoders keep the whole ADTS stream in RAM here, so the "largest stream" is a property of this harness (clip must fit the largest
block less a 64 KB reserve), not of the decoder; in a streaming player only state and stack matter.

| Decoder | Heap, LC / HE | Stack used, LC / HE | Largest stream resident, LC / HE | Speed LC / HE (indicative) | RAM left of the 257 KB for the app (LC / HE) |
|---|---|---|---|---|---|
| libhelix | 79 / 79 KB | 1.5 / 1.5 KB | 44.8 / 44.8 KB | 13.1x / 3.3x | about 176 / 176 KB |
| FAAD2 | 27 / 120 KB | 46 / 58 KB | 44.8 / 41.2 KB (crashes at 41.5 to 43.3 KB) | 9.4x / 2.9x | about 184 / 79 KB |
| FAAD3, single block | does not start | | | | |
| FAAD3, split allocations (`faad3s`) | 60 / 135 KB | 1.5 / 6.3 KB | 44.8 / 30.7 KB | 15.8x / 5.5x | about 195 / 116 KB |

* FAAD2 and FAAD3 allocate their SBR state when the first SBR frame arrives, so the heap grows from the LC to the HE-AAC figure only if
  the stream is HE-AAC. FAAD2's 58 KB stack is the largest demand of the three and exceeds a typical task stack (the benchmark uses 64 KB).
* The stream sizes above are about 2.8 s at 128 kbps or 9 s at 40 kbps.
* "RAM left" is 257 KB minus decoder heap and stack; the speeds come from the smoke clips (32 or 48 kHz stereo), not the gate set.
* Tested sample rates are 16 to 48 kHz and channels up to 2; the FAAD2 and FAAD3 builds are capped at 2 channels.

</details>

## Method, builds and versions

<details>
<summary>How the decoders are measured</summary>

* The firmware (`scripts/esp32/firmware_dec/`, ESP-IDF app, one build per codec and target) takes an
  ADTS clip over UART0, stores it in PSRAM (S3) or internal RAM (smaller chips), and decodes it
  `--loops` times. Only the decoder calls are timed, with the CPU cycle counter; the best pass
  is reported. CPU frequency is fixed (240 MHz on S3/ESP32, 160 MHz on C6), one task, default
  interrupts, `-O2`.
* **xRT** = source audio duration / best decode time. **MHz for 1x RT** = cycles per second of
  audio. For HE-AAC the duration is the source duration, not the doubled SBR output.
* **Heap** is the internal-RAM low-water delta since before the decoder was opened (decoder state; it is an all-time minimum, so a larger earlier request can inflate it), **stack** is
  `65536 - high-water free` of the 64 KB main task, **flash** is the codec archive's
  `.text + .rodata + .data` from the IDF map.
* `--verify` also returns the PCM and compares it with ffmpeg's decode of the same stream
  (lag found by cross-correlation, SNR over the overlap). Every run also returns a PCM CRC32.
* The bitstreams are the FAAC 2.1.0 LC and HE-AAC v1 outputs already in
  `comparison_results.json` (M4A, remuxed to ADTS with ffmpeg), so every decoder sees the same
  streams. The 5.1 scenarios have no corpus here and are not covered.
* The boards are reached through their USB-UART bridges (UART0, 921600 8N1), not native USB.

</details>

<details>
<summary>Codec builds</summary>

| | libhelix-aac | FAAD2 | FAAD3 |
|---|---|---|---|
| Source | `../codec-helix/src/libhelix-aac` (C API, not the C++ wrapper) | `../faad2/libfaad` | `libfaad/` + `common/` of faac `origin/stack-tests`, exported read-only by `fetch_faad3.sh` |
| Arithmetic | fixed point | fixed point | float (also uses `double` in places; not audited) |
| Config | SBR on (`HELIX_FEATURE_AUDIO_CODEC_AAC_SBR`), generic-C `MULSHIFT32` (no Xtensa/RISC-V asm) | `FIXED_POINT`, `LC_ONLY_DECODER`, SBR on, `dontUpSampleImplicitSBR=1`, 16-bit output | new `faad.h` API, ADTS, 16-bit output, SBR on, PS off (`FAAD_DISABLE_PS`), `MAX_CHANNELS=2`, hand-written `config.h` |
| State | allocated from internal RAM | `malloc` (internal RAM) | caller-owned block from `faad_get_state_size()` in internal RAM |
| Notes | | fixed-point + `SBR_LOW_POWER` does not compile upstream, so SBR builds keep the PS code (never exercised, HE v1 streams only); needs ~57 KB of stack | WIP branch, so numbers can move |
| Not supported | PS, Main, LTP, LD | Main/SSR (auto-disabled in fixed point) | ADIF, LATM, DRM, Main/SSR/LTP/LD |

FAAD2 with `dontUpSampleImplicitSBR=0` (the default) runs implicit SBR on plain LC streams at
rates of 24 kHz and below, doubling the output rate and making it about 12x slower than Helix on
mono speech; this benchmark turns it off so all decoders decode the same LC stream.

</details>

<details>
<summary>Tested versions</summary>

| Component | Version |
|---|---|
| ESP-IDF | v5.5.5 (pinned by `setup_idf.sh`) |
| Compilers | xtensa-esp-elf and riscv32-esp-elf GCC 14.2.0 (`esp-14.2.0_20260121`) |
| esptool | 5.5.0 |
| pyserial / ffmpeg (reference decode, remux) | 3.5 / 7.1.5 (Debian 13) |
| libhelix-aac | `nschimme/codec-helix` `a2e7bfc` (branch `fix-aac-corrupt-stream-overflows`), sources used from `src/libhelix-aac` |
| FAAD2 | `2.11.4-1-g864ccb5` (`../faad2`, master) |
| FAAD3 | faac `origin/stack-tests` @ `bd73c46c` (WIP, exported by `fetch_faad3.sh`; the library reports `3.0.0`) |
| FAAD3, split allocations (`faad3s`) | branch `faad3-split` @ `95b50803` ([nschimme/faac#36](https://github.com/nschimme/faac/pull/36)), on top of `bd73c46c`; exported with `fetch_faad3.sh <worktree> faad3-split-src` |
| Bitstreams | FAAC 2.1.0 (`3d077533`), LC and HE-AAC v1, from `comparison_results.json` |
| faac-benchmark | `bdbffd8` plus the uncommitted `scripts/esp32/` work |

Boards: ESP32-S3 (QFN56 rev v0.2, 8 MB octal PSRAM, 240 MHz), ESP32-C6 (QFN40 rev v0.2,
160 MHz), ESP32 WROOM (ESP32-D0WDQ6 rev v1.0, 240 MHz, CP2102 bridge).

</details>

<details>
<summary>Decoder caveats</summary>

* One clip is decoded at a time with the clip held in memory, so the numbers are decode cost only.
  The S3 input sits in 80 MHz octal PSRAM, which can cost a little through the cache; decoder state
  and the PCM scratch buffer are in internal RAM.
* All decoders are generic C on these chips (libhelix has no Xtensa MAC or RISC-V assembly), so the
  ratios compare compiler output rather than tuned kernels; FAAD3 is float code and depends on the chip's
  FPU (the S3 and ESP32 have a single-precision one, the C6 none). They would shift with `-Os`, LTO or an
  ESP-IDF update.
* The gate set is 4 clips per scenario, not the full corpus; a full run is a rerun without `--gate`
  (resumable).
* FAAD2's per-pass decoder init (allocation, tables) is inside the timed region, as it is for real
  use on a short clip; libhelix's and FAAD3's init is outside it.
* Licences: libhelix is RealNetworks RPSL/RCSL and FAAD2 is GPL; the setup script fetches nothing from
  either, it only reads the sibling checkouts. `fetch_faad3.sh` only exports files from your own faac checkout.
* Adding another decoder: one firmware adapter in `scripts/esp32/firmware_dec/components/codec_<name>/`
  plus an entry in the `CODECS` table of `esp32_dec_bench.py`. The FAAC encoder is a separate tool (see below).

</details>
