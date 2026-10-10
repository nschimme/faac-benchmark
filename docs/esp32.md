# On-device AAC benchmarks: decoders and the FAAC encoder (ESP32 family)

Decode speed, memory and footprint of AAC decoders on real ESP32 silicon, so the numbers can sit next
to the host `compare_codecs.py` decoder results: **libhelix-aac** and **FAAD2 (`FIXED_POINT`)** (both fixed
point) and the work-in-progress **FAAD3** (float) from the `stack-tests` branch of the faac repo. A second
tool runs the **FAAC encoder** the same way. The **ESP32-S3 is the benchmark target**; the ESP32-C6 and the
classic ESP32 run the same gate set, but only the clips that fit their internal RAM.

**Contents:** [Executive summary](#executive-summary) · [Quick start](#quick-start) ·
[Decoder results](#decoder-results-esp32-s3--240-mhz) · [HE-AAC v2](#he-aac-v2-parametric-stereo) · [FAAD3 profile](#where-faad3-spends-its-cycles-profile) · [FAAC encoder](#faac-encoder) · [Classic ESP32 limits](#classic-esp32-limits-no-psram) ·
[Method, builds and versions](#method-builds-and-versions). Long tables and explanations are folded
into collapsible sections; expand them as needed.

## Executive summary

**Is FAAD3 better?** On the ESP32-S3 it beats FAAD2 on speed, flash, stack and total RAM; against libhelix it is
still a trade, not a win; and on the FPU-less C6 it is the weakest of the three. Measured at 240 MHz
over the gate set (4 clips per scenario, 27 scenarios, FAAC 2.1.0 LC and HE-AAC v1 streams, plus the fdkaac HE-AAC v2 streams of the same
scenarios for Parametric Stereo; FAAD3 is built with PS):

| | libhelix (fixed) | FAAD2 (fixed) | FAAD3 (float, WIP) |
|---|---|---|---|
| LC speed, 44.1/48 kHz stereo (x realtime) | **12.1 to 19.0** | 6.8 to 8.6 | 9.6 to 12.3 |
| HE-AAC v1 speed, 44.1/48 kHz stereo (x realtime) | 3.6 to 3.9 | 1.8 to 2.0 | **4.2 to 4.7** |
| MHz for real-time HE-AAC v1 | 60 | 119 | **51** |
| HE-AAC v2 (PS), 44.1/48 kHz stereo (x realtime) | 6.6 to 7.7, but **mono**: no PS, core only | **fails**: device stops responding (23 of 23 clips tried) | **4.3 to 4.8**, stereo, 102 to 105 dB SNR vs ffmpeg |
| Flash | 78 KB | 159 KB | **66 KB** |
| RAM: heap + static + stack | **about 80 KB** | about 175 KB | about 156 KB |
| HE-AAC speed with state in PSRAM, vs internal RAM (S3) | **1.00x** | 0.92x | 0.88x |
| Real time on an ESP32-C6 (no FPU), LC / HE-AAC | **yes / yes** (9 to 19x / 2.3 to 3.5x) | yes / yes (5 to 9x / 1.7 to 2.4x) | **no / no** (0.8 to 1.4x / 0.24 to 0.31x) |
| Starts on a classic ESP32 (no PSRAM) | yes | yes | yes (split allocations are now in `stack-tests`) |

* **FAAD3 vs FAAD2:** about 1.4x faster for LC and 2.4x faster for HE-AAC, 2.4x less flash and about 8x less
  stack (7 vs 57 KB), and about 11% less total RAM; output matches ffmpeg as closely as FAAD2's. On HE-AAC v2 FAAD2
  never answered on the S3 while FAAD3 decodes every clip. On this evidence FAAD3 is the better FAAD.
* **FAAD3 vs libhelix:** libhelix decodes LC about 1.4x faster and needs about half the RAM; FAAD3
  decodes HE-AAC v1 about 1.2x faster, uses 15% less flash, has a cleaner API, and is the only one of the three that
  decodes HE-AAC v2 (libhelix has no PS and returns the mono core, which is why its v2 speed looks high). LC on a small part: libhelix. HE-AAC
  where flash is the constraint: FAAD3. libhelix is the only one comfortable on every chip tested.
* **Where FAAD3 struggles:** no FPU (C6) makes it slow: stereo LC lands between 0.8x and 1.4x real time and HE-AAC
  at 0.24 to 0.31x. Its state is no longer one 131 KB block (the split allocations of
  [PR #36](https://github.com/nschimme/faac/pull/36) are in `stack-tests`), so the classic ESP32 can run it.
* **Parametric Stereo:** PS costs FAAD3 11 KB of flash (55 to 66 KB) and 17 KB of static RAM (36 to 53 KB); decode speed on
  HE-AAC v1 and the decoded PCM are unchanged (CRC identical on all 196 FAAC clips, which contain no PS). FAAC cannot
  produce HE-AAC v2 yet (`FAAC_OBJ_HE_AAC_V2` returns `FAAC_ERR_UNSUPPORTED`), so PS is exercised with fdkaac streams
  (see [HE-AAC v2](#he-aac-v2-parametric-stereo)). The C6 and classic ESP32 tables below were measured before PS was
  enabled.
* **Read with care:** FAAD3 is a work-in-progress branch (`171671bb`) capped at 2
  channels, which flatters its footprint; it is float code and any `double` in it is software-emulated. FAAD2's
  figures include PS code. Gate subset, not the full corpus. The C6 and classic ESP32 only decode the
  clips that fit their internal RAM (see the coverage tables).

**Classic ESP32 (no PSRAM, 257 KB free internal RAM, largest block 108 KB):** the clip is streamed to the board in blocks, so
any bitstream can be decoded and the limit is the decoder's own RAM. All three decoders run LC and HE-AAC v1 in real time (see
[Classic ESP32 limits](#classic-esp32-limits-no-psram) for the coverage); FAAC encodes LC stereo at 4.0 to 10.3x and mono at 18 to 29x, but
HE-AAC v1 stereo does not fit (out of memory), even with the 116 KB state of the current libfaac.

**FAAC encoder on the S3** (libfaac `171671bb`, one clip per scenario, 51 successful encodes, all decoded by ffmpeg):
* **Speed:** 44.1/48 kHz stereo LC encodes at 4.6 to 10.4x real time (about 23 to 52 MHz per real-time stream); HE-AAC v1 at 4.7 to 7.1x;
  16/24 kHz mono LC at 25 to 38x.
* **Memory:** 56 KB (mono) to 89 KB (stereo) of internal RAM for LC and 116 KB for HE-AAC v1 stereo, down from 70 / 103 / 158 KB with the
  libfaac of the previous run; 43 KB of flash.
* **Limits, all traced to libfaac itself (details in the encoder section):** no HE-AAC v1 below 32 kHz, so the 5 mono speech scenarios are
  LC only; LC at 8 kbps per channel overshoots (22 kbps for a 16 kbps target, a quality floor that its auto mode avoids by picking
  HE-AAC); HE-AAC v1 at 48 kHz cannot exceed the AAC core limit of 288 kbps stereo, so the 320 kbps scenario lands at 264. The C6 cannot
  encode stereo in real time (0.4 to 0.8x LC, 0.2 to 0.35x HE-AAC). No quality metric was computed.

## Quick start

Everything lives in `scripts/esp32/` and runs from the repo root:

```
scripts/esp32/setup_idf.sh                                     # once: pinned ESP-IDF under scripts/esp32/.toolchain
scripts/esp32/fetch_faad3.sh [ref]                             # once: snapshot of FAAD3 (default origin/stack-tests)
.venv/bin/python scripts/esp32/esp32_dec_bench.py build  --target esp32s3
.venv/bin/python scripts/esp32/esp32_dec_bench.py run    --target esp32s3 --port /dev/serial/by-id/<board> --gate --verify 16
.venv/bin/python scripts/esp32/esp32_dec_bench.py report --target esp32s3
```

<details>
<summary>Decoder tool flags</summary>

Flags: `--codec helix,faad2,faad3` (append `-psram`, e.g. `faad3-psram`, to allocate the decoder state from PSRAM on the S3), `--scenarios a,b`, `--gate` (fixed gate clips, 4 per scenario),
`--limit N`, `--loops N` (decode passes per clip, best is reported; default 3),
`--verify N` (return PCM and SNR-check about 1 clip in N), `--max-bytes`, `--encoder`
(row-key substring of the encoder whose bitstreams are used, default `faac`),
`--profiles lc,he`, `--sbr 0|1` (build with or without SBR), `--stream auto|on|off` (feed the clip to the device in blocks instead of
holding it; `auto` streams on the chips without PSRAM). Results are saved after every
clip to `results/esp32_dec_<target>.json` and a rerun skips what is already there. Bitstreams the host does not
send (too big for the target) are recorded as `skipped` rows, so `report` can list the limits of each target in its
Outcomes table. Reference list of the pure helpers is in `docs/scripts.md`; unit tests (no hardware) are
`tests/test_esp32_dec_bench.py`.

</details>

## Decoder results: ESP32-S3 @ 240 MHz

Gate set: 4 clips per scenario, 27 scenarios, LC and HE-AAC v1 bitstreams, best of 3 passes.
All 196 bitstreams per decoder were decoded without error (588 decodes), `results/esp32_dec_esp32s3.json`.

| Codec | Profile | Bitstreams | Mean xRT | Median xRT | Min xRT | MHz for 1x RT (mean / worst) |
|---|---|---|---|---|---|---|
| libhelix | LC | 108 | 30.4 | 18.7 | 11.7 | 11.9 / 20.6 |
| libhelix | HE | 88 | 4.1 | 3.8 | 3.6 | 59.9 / 67.2 |
| FAAD2 | LC | 108 | 14.5 | 8.2 | 6.7 | 24.5 / 35.6 |
| FAAD2 | HE | 88 | 2.0 | 1.9 | 1.8 | 119.4 / 130.6 |
| FAAD3 | LC | 108 | 20.4 | 11.8 | 9.3 | 17.3 / 25.9 |
| FAAD3 | HE | 88 | 4.8 | 4.5 | 4.2 | 50.6 / 57.6 |

The LC means are pulled up by the 16 and 24 kHz mono speech scenarios (Helix about 99x and 67x);
on 44.1/48 kHz stereo music LC runs 12.1 to 19.0x (Helix), 9.6 to 12.3x (FAAD3) and 6.8 to 8.6x (FAAD2).
Ranges are over the per-scenario means.

* **LC:** Helix is the fastest, about 1.4x faster than FAAD3 and 2x faster than FAAD2.
  FAAD3 is about 1.4x faster than FAAD2.
* **HE-AAC v1:** FAAD3 is the fastest (about 1.2x faster than Helix and 2.4x faster than FAAD2),
  so the ranking flips; it needs about 51 MHz for real-time HE-AAC, Helix about 60 MHz, FAAD2
  about 119 MHz. On stereo music HE-AAC costs 2.5 to 4x the LC time, since SBR doubles the output rate.
* The S3 has a single-precision FPU, which suits FAAD3's float arithmetic; the C6 has none (see the
  C6 and ESP32 section), and any `double` in FAAD3 is done in software on all three chips.

<details>
<summary>By scenario (mean xRT, all 27 scenarios)</summary>

| Scenario | Helix LC | FAAD2 LC | FAAD3 LC | Helix HE | FAAD2 HE | FAAD3 HE |
|---|---|---|---|---|---|---|
| 16k_mono_20k | 99.4 | 47.3 | 65.0 | - | - | - |
| 16k_mono_24k | 97.9 | 47.1 | 64.7 | - | - | - |
| 16k_mono_voip_24k | 93.4 | 45.6 | 63.7 | - | - | - |
| 24k_mono_28k | 67.2 | 31.8 | 43.6 | - | - | - |
| 24k_mono_32k | 66.7 | 31.7 | 43.5 | - | - | - |
| 32k_stereo_16k | 28.5 | 12.2 | 16.8 | 5.3 | 2.6 | 6.3 |
| 32k_stereo_48k | 24.5 | 11.5 | 16.4 | 5.1 | 2.5 | 6.2 |
| 32k_stereo_64k | 22.9 | 11.2 | 16.0 | 5.1 | 2.5 | 6.2 |
| 32k_stereo_80k | 22.2 | 11.1 | 15.8 | 5.1 | 2.5 | 6.2 |
| 32k_stereo_96k | 21.5 | 11.0 | 15.6 | 5.0 | 2.5 | 6.1 |
| 44k1_stereo_64k | 18.7 | 8.6 | 12.3 | 3.9 | 2.0 | 4.7 |
| 44k1_stereo_128k | 16.3 | 8.1 | 11.6 | 3.9 | 1.9 | 4.7 |
| 44k1_stereo_160k | 15.5 | 8.0 | 11.3 | 3.8 | 1.9 | 4.6 |
| 44k1_stereo_192k | 14.8 | 7.8 | 11.1 | 3.8 | 1.9 | 4.6 |
| 44k1_stereo_256k | 13.7 | 7.6 | 10.7 | 3.7 | 1.9 | 4.5 |
| 48k_stereo_24k | 19.0 | 8.1 | 11.2 | 3.9 | 1.9 | 4.4 |
| 48k_stereo_32k | 18.9 | 8.1 | 11.4 | 3.9 | 1.9 | 4.5 |
| 48k_stereo_40k | 18.9 | 8.1 | 11.6 | 3.8 | 1.9 | 4.4 |
| 48k_stereo_48k | 18.7 | 8.1 | 11.7 | 3.8 | 1.9 | 4.4 |
| 48k_stereo_56k | 18.6 | 8.1 | 11.7 | 3.8 | 1.9 | 4.4 |
| 48k_stereo_64k | 17.9 | 8.0 | 11.5 | 3.8 | 1.9 | 4.4 |
| 48k_stereo_96k | 16.0 | 7.7 | 11.0 | 3.8 | 1.9 | 4.4 |
| 48k_stereo_128k | 15.3 | 7.5 | 10.8 | 3.7 | 1.9 | 4.4 |
| 48k_stereo_160k | 14.6 | 7.4 | 10.6 | 3.7 | 1.9 | 4.3 |
| 48k_stereo_192k | 14.0 | 7.3 | 10.4 | 3.6 | 1.9 | 4.3 |
| 48k_stereo_256k | 13.0 | 7.1 | 10.0 | 3.6 | 1.8 | 4.2 |
| 48k_stereo_320k | 12.1 | 6.8 | 9.6 | 3.6 | 1.8 | 4.2 |

</details>

<details>
<summary>Memory and footprint (heap, stack, flash on all three chips)</summary>

| Codec | Decoder heap (KB, peak; at open) | Main-task stack used (KB, max) | Flash (.text + .rodata + .data) | Static RAM |
|---|---|---|---|---|
| libhelix | 79 (open: 78) | 1.6 | 78,249 B (text 37,153, rodata 41,096) | 4 B |
| FAAD2 | 117 (open: 1) | 56.5 | 159,000 B (text 103,850, rodata 53,772) | 1,386 B |
| FAAD3 (with PS) | 97 (open: 48) | 6.4 | 66,525 B (text 52,582, rodata 13,943) | 52,920 B |

All three include SBR, and FAAD2 and FAAD3 include PS (libhelix has none). FAAD3 is capped at 2 channels (`MAX_CHANNELS=2`), so its footprint is the smallest partly through
build choices. Enabling PS grew FAAD3 from 55,301 B of flash and 36,044 B of static RAM; the heap peak did not move for the FAAC streams. FAAD3 also has about 53 KB of static RAM (`.bss`), which the other two do not; on HE-AAC v2 streams its heap peak is 119 KB. The heap figure is the internal-RAM
low-water delta over the whole run, so it is the peak over all profiles; FAAD3's state is now allocated as several blocks
(48 KB at open, rising to 97 KB, consistent with the SBR state being allocated on the first SBR frame; not checked in the
source), which is why its heap is lower than the 131 KB single block measured before. The same libraries on the other chips (flash):

| Chip | libhelix | FAAD2 | FAAD3 |
|---|---|---|---|
| ESP32-S3 (Xtensa LX7) | 78,249 B | 159,000 B | 66,525 B (with PS) |
| ESP32-C6 (RISC-V) | 82,028 B | 145,626 B | 63,736 B (before PS) |
| ESP32 (Xtensa LX6) | 78,078 B | 142,902 B | 55,142 B (before PS) |

</details>

<details>
<summary>Correctness check against ffmpeg (11 clips verified by all three decoders)</summary>

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
| HE | 48k_stereo_192k | 21-classic | 81.5 | 75.8 | 104.4 |
| LC | 44k1_stereo_64k | sandman | 19.3 | 19.3 | 19.3 |
| LC | 44k1_stereo_128k | sandman | 30.0 | 30.1 | 30.0 |
| LC | 48k_stereo_128k | 21-classic | 50.6 | 50.5 | 50.6 |

The three decoders agree with each other to about 0.2 dB on every clip except one (HE, `48k_stereo_192k`), where FAAD3
(float) comes closest to ffmpeg. SNR against ffmpeg falls with bitrate (50 dB at 128 kbps, 10 to 16 dB
at 24 to 32 kbps), so the low values are a property of the stream and not of one decoder; the likely
cause is noise substitution that decoders render differently, but that has not been checked. This is
a sanity check that the on-device output is sound, not a conformance result: the host harness's
60 dB gate is the stricter test. These are the same bitstreams as the previous run, so the table is unchanged by the newer
decoder builds.

</details>

<details>
<summary>State in PSRAM instead of internal RAM (S3)</summary>

Same bitstreams, decoder state (and, for FAAD2, every `faad_malloc`) allocated from 80 MHz octal PSRAM instead of internal RAM; the
clip and the PCM scratch buffer stay where they were. Mean xRT over the clips that completed in both modes; the ratio column is speed in PSRAM divided by speed internal (below 1.00x means PSRAM is slower).

| Codec | Profile | Bitstreams | xRT internal | xRT PSRAM | PSRAM speed / internal | Internal heap peak, KB (internal vs PSRAM) | CRC identical |
|---|---|---|---|---|---|---|---|
| libhelix | LC | 108 of 108 | 30.4 | 30.0 | 0.99x | 79 vs 50 | 108/108 |
| libhelix | HE | 88 of 88 | 4.1 | 4.1 | 1.00x | 79 vs 50 | 88/88 |
| FAAD2 | LC | 108 of 108 | 14.5 | 14.5 | 1.00x | 117 vs 0 | 108/108 |
| FAAD2 | HE | 88 of 88 | 2.0 | 1.9 | 0.92x | 117 vs 0 | 88/88 |
| FAAD3 | LC | 108 of 108 | 20.4 | 20.3 | 1.00x | 97 vs 16 | 108/108 |
| FAAD3 | HE | 88 of 88 | 4.8 | 4.3 | 0.88x | 97 vs 16 | 88/88 |

* **libhelix and FAAD2 LC cost nothing** (about 1.00x) and the decoded PCM is bit-identical (CRC equal on every clip). libhelix HE-AAC
  is also free (1.00x); FAAD2 HE-AAC loses about 8% (0.92x), because SBR walks much more memory per frame.
* libhelix keeps about 50 KB in internal RAM in this mode, so some of its allocations do not go through the switch; that was not chased.
* **FAAD3 LC is free too (1.00x); HE-AAC costs about 12% (0.88x)**, still 3.6 to 4.1x real time on stereo music (about 57 MHz for 1x). Its
  internal-RAM heap drops from 97 KB to 16 KB. This needed a build change: `stack-tests` allocates its state inside the library, so the
  old caller-owned-block switch moved nothing (an earlier pass of this table showed identical rows); the adapter now overrides libfaad's compile-time
  `AllocMemory` / `FreeMemory` macros to allocate from PSRAM.
* So a build short of internal RAM can put a decoder's state in PSRAM: for LC it is free, and for HE-AAC libhelix keeps its speed,
  FAAD3 loses 12% and FAAD2 8%.
* The first pass of this run lost 15 HE-AAC and FAAD2 stereo clips (FAAD2 "no response magic", FAAD3 "no frames decoded"). They were exactly the clips
  that return PCM for the SNR check: the harness gave the PCM return buffer the whole largest free PSRAM block, leaving nothing for state that is
  allocated lazily (FAAD3's SBR state, FAAD2's per-frame buffers). The harness now leaves 1 MB free, and all 15 clips decode, so they are not a decoder fault.

</details>

<details>
<summary>ESP32-C6 and classic ESP32 gate runs</summary>

Both boards run the same gate bitstreams as the S3. The **classic ESP32 streams the clip** to the board in blocks (`--stream auto`,
see [Method](#method-builds-and-versions)), so every bitstream is decoded and the population is the S3's. The **C6 table is from the
earlier stored-clip run**: only clips that fit the chip's internal RAM were sent (cap 90,000 B), the rest were recorded as skipped,
and the device refused or ran out of memory on some, so its speeds are over a smaller, different population than the S3's. A streaming
sweep of the C6 is still running; it writes to `results/esp32_dec_esp32c6.json`, the table here is from
`results/esp32_dec_esp32c6_resident.json`.

**ESP32-C6 @ 160 MHz** (93/93 libhelix and 93/93 FAAD2 PCM CRCs equal to the S3's; FAAD3 0/70):

| Codec | Profile | Decoded | Stereo xRT | Mono xRT | Stack (KB) |
|---|---|---|---|---|---|
| libhelix | LC | 59 | 8.9 to 18.6 | 39.9 to 63.8 | 1.3 |
| libhelix | HE | 34 | 2.3 to 3.5 | - | 1.3 |
| FAAD2 | LC | 59 | 5.0 to 9.4 | 22.3 to 35.2 | 56.1 |
| FAAD2 | HE | 34 | 1.7 to 2.4 | - | 56.1 |
| FAAD3 | LC | 59 | 0.8 to 1.4 | 4.3 to 7.3 | 6.1 |
| FAAD3 | HE | 11 | 0.2 to 0.3 | - | 6.1 |

Outcomes (every bitstream tried):

| Codec | Profile | Bitstreams | ok | Not ok (reason: count) |
|---|---|---|---|---|
| libhelix | LC | 60 | 59 | pcm buffer overflow: 1 |
| libhelix | HE | 40 | 34 | pcm buffer overflow: 6 |
| FAAD2 | LC | 60 | 59 | pcm buffer overflow: 1 |
| FAAD2 | HE | 40 | 34 | no response magic from device: 6 |
| FAAD3 | LC | 60 | 59 | pcm buffer overflow: 1 |
| FAAD3 | HE | 40 | 11 | no response magic from device: 27, pcm buffer overflow: 2 |

* **Fixed-point decoders are bit-exact across chips:** every libhelix and FAAD2 decode that ran on the C6 has the same PCM CRC32 as the S3's.
* **FAAD3 on the C6 is correct but slow.** The C6 has no FPU, so FAAD3's float code runs in software: stereo LC at 0.8 to 1.4x
  real time and HE-AAC at 0.2 to 0.3x. Its output is sound but not bit-identical to the S3's (different float rounding, 0 of 70 CRCs
  equal). The "no response magic" failures for FAAD3 are decodes (mostly HE-AAC) that exceed the host's wait, not crashes. The C6 can run
  libhelix and FAAD2 in real time but not FAAD3.

**Classic ESP32 @ 240 MHz** (196/196 libhelix, 196/196 FAAD2 and 196/196 FAAD3 PCM CRCs equal to the S3's):

| Codec | Profile | Decoded | Stereo xRT | Mono xRT | Stack (KB) |
|---|---|---|---|---|---|
| libhelix | LC | 108 | 8.3 to 24.5 | 50.7 to 80.9 | 1.5 |
| libhelix | HE | 88 | 2.9 to 4.5 | - | 1.5 |
| FAAD2 | LC | 108 | 4.9 to 10.7 | 24.9 to 39.2 | 56.4 |
| FAAD2 | HE | 88 | 2.0 to 3.0 | - | 56.4 |
| FAAD3 | LC | 108 | 8.6 to 16.8 | 40.7 to 63.2 | 6.3 |
| FAAD3 | HE | 88 | 3.6 to 5.5 | - | 6.3 |

Outcomes (every bitstream tried):

| Codec | Profile | Bitstreams | ok | Not ok (reason: count) |
|---|---|---|---|---|
| libhelix | LC | 108 | 108 | - |
| libhelix | HE | 88 | 88 | - |
| FAAD2 | LC | 108 | 108 | - |
| FAAD2 | HE | 88 | 88 | - |
| FAAD3 | LC | 108 | 108 | - |
| FAAD3 | HE | 88 | 88 | - |

* **All three decoders run every bitstream, in real time**, including FAAD3, which with the split allocations no longer needs one
  contiguous block (it did not start at all with the old single 131 KB block). 588 of 588 decodes completed, with clips up to 400 KB
  that the stored-clip harness could not hold (it managed 39 of 196 per decoder).
* **Output is bit-identical to the S3's on all 588 decodes** (float results match because both chips have a hardware FPU).
* **Heap equals the S3's** (79 / 116 / 97 KB for libhelix / FAAD2 / FAAD3; the earlier 91 / 162 / 118 KB were inflated by the clip and
  the PCM buffer sharing the heap), stack as on the S3. The only limit left is the decoder's own RAM, so the classic ESP32 is a full
  decode target; speed is 5 to 25x for stereo LC and 2 to 5.5x for HE-AAC.

</details>

<details>
<summary>ESP32-S3 vs classic ESP32 on identical clips (both 240 MHz)</summary>

Cycles for the same bitstream and the same decoder (best of 3, S3 divided by ESP32; below 1.0 means the S3 needs fewer cycles),
over every bitstream the classic ESP32 decoded:

| Decoder | Profile | Clips | S3 / ESP32 cycles |
|---|---|---|---|
| libhelix | LC | 108 | 0.64 to 0.85 |
| libhelix | HE | 88 | 0.80 to 0.84 |
| FAAD2 | LC | 108 | 0.70 to 0.86 |
| FAAD2 | HE | 88 | 1.07 to 1.15 |
| FAAD3 | LC | 108 | 0.91 to 0.98 |
| FAAD3 | HE | 88 | 0.89 to 0.90 |

The S3 is the same speed or faster in most cases, as expected for the same clock and a newer core. FAAD2 HE-AAC
is the exception: the S3 needs 7 to 15% more cycles for it, and the cause has not been found. Differences between the
two builds that could matter are the S3's PSRAM-resident input clip, its larger data cache and its cache line size; none
was isolated.

</details>

## HE-AAC v2 (Parametric Stereo)

FAAC has no HE-AAC v2 encoder (`FAAC_OBJ_HE_AAC_V2` returns `FAAC_ERR_UNSUPPORTED`, and `--object-type` accepts only `lc`,
`he-aac-v1` and `auto`), so its streams never contain PS and the FAAD3 PS code is not touched by the FAAC runs above (output CRCs equal the
no-PS build on every clip). PS pays off at tiny stereo bitrates, so the benchmark's own `hev2` rows were used: the fdkaac 1.0.6 HE-AAC v2
bitstreams of the same 22 stereo scenarios (`ffprobe` reports `HE-AACv2`, 4 gate clips each, 88 streams), run with
`esp32_dec_bench.py run --encoder fdkaac --profiles hev2 --gate --verify 16`. Same S3 setup as above.

| Codec | Decoded | Output | Mean xRT | Median xRT | Min xRT | MHz for 1x RT (mean / worst) | Heap peak (KB) | SNR vs ffmpeg (dB, 5 clips) |
|---|---|---|---|---|---|---|---|---|
| libhelix | 88 of 88 | **mono** (no PS) | 7.6 | 7.1 | 6.6 | 32.1 / 36.4 | 79 | 12.9 to 43.6 (mono core vs stereo reference) |
| FAAD2 | 0 of 23 tried | no response | - | - | - | - | - | - |
| FAAD3 (PS) | 88 of 88 | stereo | 5.0 | 4.6 | 4.3 | 49.6 / 55.9 | 119 | 101.8 to 105.0 |
| FAAD3 (PS), state in PSRAM | 88 of 88 | stereo | 4.3 | 4.0 | 3.7 | 57.0 / 65.6 | 16 | 101.8 to 105.0 (CRC identical to internal, 88/88) |

* **FAAD3 decodes PS correctly:** about 102 to 105 dB against ffmpeg on every verified clip, i.e. near bit-exact, in stereo, at 4.3 to 4.8x
  real time on 44.1/48 kHz (6.1 to 6.7x at 32 kHz). The 48 kHz stereo scenarios at 24 to 32 kbps run at 4.4 to 4.6x, so PS costs about
  the same as HE-AAC v1 on the same rate (4.5x). Its heap peak rises to 119 KB (HE v1: 97 KB) for the PS state.
* **libhelix is only fast because it skips PS:** it returns the mono core of every v2 stream (1 channel, SNR against the stereo reference
  of 13 to 44 dB), so its 7.6x is not a like-for-like decode. For a real stereo result on v2 content libhelix is not an option.
* **FAAD2 (`FIXED_POINT`) does not work on v2 streams here:** the device stopped responding on every clip tried (23 of 23, each a ~2 minute
  timeout, so the run was stopped; the other 65 were not attempted). Cause not investigated; it compiles PS in, and it is the same
  build that decodes the FAAC HE v1 streams.
* **Cost of enabling PS in FAAD3:** +11 KB flash (55,301 to 66,525 B) and +17 KB static RAM; no effect on LC or HE v1 speed
  (CRC-identical output, FAAC clips). The PSRAM mode costs 13% on v2 (0.87x), like HE v1 (0.88x).
* Not done: C6 and classic ESP32 with PS, and a no-PS FAAD3 on the v2 streams (to quantify what is lost without PS).

## Where FAAD3 spends its cycles (profile)

FAAD3 is slower than libhelix on LC (about 1.4x on the S3) and far slower on the FPU-less C6, so a per-function cycle profile
was taken with `scripts/esp32/esp32_dec_profile.py`: the FAAD3 sources are built with `-finstrument-functions`
(`CODEC_PROFILE=1`), the adapter charges exclusive cycles to each function, and the host prints the table after one decode pass.
The hooks cost about 50 cycles per instrumented call, so the ranking of the large functions is reliable and the small ones read
high; the smallest per-sample helpers (`pcm_to_s16`, the Huffman and bit-reader helpers) are excluded, so their time is charged to
their callers, and anything the compiler inlines shows up in its caller. Short clips (1 to 2.5 s of 48 kHz stereo sandman, LC
128 kbps and HE-AAC v1 64 kbps) that fit the chips' internal RAM; each profile is one pass.

```
.venv/bin/python scripts/esp32/esp32_dec_profile.py --target esp32 --port /dev/ttyUSB0 clip.aac
```

<details>
<summary>Classic ESP32 @ 240 MHz (has an FPU): HE-AAC v1 and LC</summary>

HE-AAC v1, 64 kbps (190 M instrumented cycles for 59 frames):

| Function | Share | Calls | Cycles per call | What it is |
|---|---|---|---|---|
| `mac64` | 15.4% | 33,984 | 740 | SBR synthesis window, 64 multiply-accumulates per call (about 11.5 cycles each) |
| `qmf_analysis_window` | 12.8% | 3,776 | 5,533 | SBR analysis window, 320 multiply-accumulates per call (about 17 cycles each) |
| `radix4_butterflies` + `radix4_first_stage` + `radix4_stage` | 18.3% | 52,684 | 600 / 1,068 / 225 | FFT kernels (QMF banks and IMDCT) |
| `faad_decode_frame` (own time, inlined helpers) | 10.8% | 59 | 299,711 | includes the float-to-int16 output loop |
| `qmf_synthesis_slot` | 6.9% | 3,776 | 2,981 | pre/post rotation around the FFT |
| `sbr_hf_adjust` | 6.5% | 118 | 89,953 | HF envelope adjustment |
| `faad_imdct_and_window` + `dct4` | 5.0% | | | core IMDCT |

LC, 128 kbps (68 M instrumented cycles for 118 frames of the same music):

| Function | Share | What it is |
|---|---|---|
| `faad_decode_frame` (own time) | 21.9% | inlined output conversion (2048 samples per channel and frame) and the rest of the frame work |
| FFT kernels (`radix4_*`) | about 14.7% | |
| `faad_decode_spectral_data` | 11.4% | Huffman decode and dequantization |
| `dct4` | 8.2% | pre/post rotation around the FFT |
| `faad_apply_pns` | 7.0% | noise fill: a random number and an int-to-float conversion per line, `powf` and `sqrtf` per band |
| `faad_imdct_and_window` + `imdct_emit*` + `imdct_sample` | about 17% | windowing, overlap-add and the fold |

</details>

<details>
<summary>ESP32-C6 @ 160 MHz (no FPU): LC</summary>

LC, 128 kbps, 1 s (190 M instrumented cycles for 47 frames): every float operation is a call into the soft-float library, so the transform kernels
dominate.

| Function | Share | Cycles per call |
|---|---|---|
| `radix4_butterflies` | 33.7% | 16,464 |
| `dct4` | 16.1% | 117,403 |
| `radix4_first_stage` | 12.5% | 91,043 |
| `imdct_emit_long` | 8.1% | 285,908 |
| `faad_apply_pns` | 7.4% | 149,792 |
| `faad_decode_frame` (own time) | 6.0% | 244,970 |
| `faad_decode_spectral_data` | 4.1% | 83,060 |

FFT and `dct4` together are about 62% of the pass, against about 23% on the ESP32, which is why the C6 runs FAAD3 at 0.8 to 1.4x real time
where fixed-point libhelix reaches 9 to 19x.

</details>

<details>
<summary>What could be done about it (not measured yet)</summary>

These follow from reading the hot code; none has been tried on a board, and the gains are estimates.

1. **Synthesis window (`mac64`):** the ten 64-tap runs each load and store `out[]`, ten passes over the same 64 floats. Accumulating
   all ten taps in registers per output stores each result once. This is the largest single HE-AAC item.
2. **Analysis window:** the loop reads `qmf_c[2 * (...)]` with stride 2 through ring-buffer pointers. A contiguous decimated copy of
   the window made at init and the five taps unrolled should remove most of the 17 cycles per multiply-accumulate.
3. **FFT:** the kernel is an out-of-place Stockham radix-4 on separate real and imaginary arrays, written for x86 SIMD. On Xtensa and
   RISC-V it has no SIMD and 14 live floats per butterfly, so it likely spills. Candidates are an in-place interleaved radix-2/4, or an
   optional hook for esp-dsp's assembly FFT (which has ESP32-S3 vector variants), at the price of a platform-specific path.
4. **Output conversion and PNS:** use the FPU's round/truncate instruction for float to int16 instead of the add-and-reinterpret trick;
   replace `powf(2, nrg / 4)` in `apply_pns` with a four-entry table and an exponent shift.
5. **Constant tables in flash:** the window and twiddle tables are read through the flash cache; copying the hot ones to RAM is a cheap test.
6. **FPU-less C6:** float cannot be tuned out of the transforms; it would need a fixed-point (Q31) FFT and QMF path, which is a larger project.

LC being slower than libhelix comes mostly from the transforms and the conversion/emit loops. FAAD3 already beats libhelix on HE-AAC, so
items 1 and 2 would widen that lead.

</details>

## FAAC encoder

An on-device benchmark of the FAAC *encoder* (libfaac from `../faac` at `171671bb`, float, `FAAC_OBJ_LOW` and
`FAAC_OBJ_HE_AAC_V1`). It is separate from the decoder tool: own firmware
(`scripts/esp32/firmware_enc/`, see its README for the wire protocol), own CLI
(`scripts/esp32/esp32_enc_bench.py`), own results (`results/esp32_enc_<target>.json`), tests in
`tests/test_esp32_enc_bench.py`. The ESP32-S3 is the benchmark target; the ESP32-C6 and classic ESP32 run the same gate list,
on shorter clips that fit their internal RAM.

```
.venv/bin/python scripts/esp32/esp32_enc_bench.py build  --target esp32s3 [--faac-dir ../faac] [--sbr-decimation N]
.venv/bin/python scripts/esp32/esp32_enc_bench.py run    --target esp32s3 --port /dev/serial/by-id/<board> --gate
.venv/bin/python scripts/esp32/esp32_enc_bench.py report --target esp32s3
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

Build notes: sources are `libfaac/*.c` without `quantize_sse.c`, plus `common/*.c` (the FFT and SBR/SFB tables shared with
libfaad, built with `COMMON_PREFIX=faac_`); `FAAC_MAX_CHANNELS` is 2 (state
scales with it, 5.1 scenarios are skipped); a stand-in `config.h` replaces meson's. The library uses
`double` in a few places (filterbank, frame, quantizer), which is software-emulated on all three
chips.

</details>

### Results: ESP32-S3 @ 240 MHz

`run --target esp32s3 --gate --loops 3`, libfaac `171671bb`, ESP-IDF v5.5.5, default 10 s clips, every bitstream returned and checked.
The gate list has **one clip per scenario** for the encoder (28 scenarios), each encoded as LC and as HE-AAC v1: 56 requested,
**51 ran, 5 not applicable** (HE-AAC v1 on 16/24 kHz mono, which libfaac does not offer, see below). Passes are deterministic: mean and
best of 3 differ by 0.1% or less. Results: `results/esp32_enc_esp32s3.json`.

By profile:

| Profile | Encodes | Mean xRT | Min xRT | MHz for 1x RT (mean / worst) | Mean actual/target kbps | Encoder heap | Stack used | ffmpeg decodes |
|---|---|---|---|---|---|---|---|---|
| HE-AAC v1 | 23 | 6.27 | 4.65 | 40 / 52 | 1.00x | 117 KB | 13.7 KB | 23/23 |
| LC | 28 | 11.71 | 4.62 | 31 / 52 | 1.03x | 89 KB | 8.9 KB | 28/28 |

Reading: a stereo 44.1/48 kHz encode needs about 33 MHz (64 kbps) to 52 MHz (320 kbps) per real-time stream, so the S3 at 240 MHz
encodes 4.6 to 10x faster than real time on one core; a 16 kHz mono voice stream needs 6 MHz. Cost grows with bitrate (more nonzero
coefficients to quantize and Huffman-code), and HE-AAC v1 costs more than LC at low and mid rates (about 1.1 to 1.35x at 24 to 128 kbps,
48 kHz) but is *not* more expensive at 192 kbps and above, where the two are within a few percent (HE is even marginally faster at 256 and 320
kbps; there libfaac clamps the HE core, see accuracy). These xRT figures are for the encode call only; they are about the same magnitude
as the S3 decoder results above (FAAD3 HE-AAC decode 4.1 to 4.6x, libhelix LC 12 to 19x); encoding HE-AAC v1 is about as fast as FAAD3
decoding it.

<details>
<summary>By family (xRT and MHz per sample-rate family)</summary>

Min to max over the family's scenarios, i.e. over bitrate; xRT falls as the bitrate rises:

| Family | Profile | Scenarios | xRT | MHz for 1x RT |
|---|---|---|---|---|
| 16 kHz mono | LC | 3 | 36.7 to 38.1 | 6 to 7 |
| 24 kHz mono | LC | 2 | 24.8 to 25.2 | 10 to 10 |
| 32 kHz stereo | LC | 5 | 7.5 to 13.6 | 18 to 32 |
| 32 kHz stereo | HE-AAC v1 | 5 | 7.6 to 10.1 | 24 to 32 |
| 44.1 kHz stereo | LC | 5 | 4.9 to 7.4 | 33 to 49 |
| 44.1 kHz stereo | HE-AAC v1 | 5 | 4.9 to 6.2 | 39 to 49 |
| 48 kHz stereo | LC | 13 | 4.6 to 10.4 | 23 to 52 |
| 48 kHz stereo | HE-AAC v1 | 13 | 4.7 to 7.1 | 34 to 52 |

</details>

<details>
<summary>By scenario (all 28 scenarios)</summary>

xRT / MHz for 1x RT and actual kbps per profile; one clip each, 10 s sandman for stereo; mono clips are only 8.6 s long in the corpus:

| Scenario | Source | LC xRT / MHz | LC kbps | HE xRT / MHz | HE kbps | Target |
|---|---|---|---|---|---|---|
| 16k_mono_20k | R_01_CLIP_MK 8.6 s | 38.06 / 6 | 21.0 | n/a | - | 20 |
| 16k_mono_24k | R_01_CLIP_MK 8.6 s | 36.67 / 7 | 22.7 | n/a | - | 24 |
| 16k_mono_voip_24k | C_01_CHOP_FA 8.6 s | 37.36 / 6 | 23.9 | n/a | - | 24 |
| 24k_mono_28k | R_01_CLIP_MK 8.6 s | 25.21 / 10 | 27.9 | n/a | - | 28 |
| 24k_mono_32k | R_01_CLIP_MK 8.6 s | 24.76 / 10 | 29.4 | n/a | - | 32 |
| 32k_stereo_16k | sandman 10.0 s | 13.61 / 18 | 17.7 | 10.11 / 24 | 17.2 | 16 |
| 32k_stereo_48k | sandman 10.0 s | 9.71 / 25 | 49.2 | 8.36 / 29 | 48.3 | 48 |
| 32k_stereo_64k | sandman 10.0 s | 8.67 / 28 | 64.9 | 8.01 / 30 | 63.6 | 64 |
| 32k_stereo_80k | sandman 10.0 s | 7.98 / 30 | 80.7 | 7.73 / 31 | 79.2 | 80 |
| 32k_stereo_96k | sandman 10.0 s | 7.51 / 32 | 96.5 | 7.58 / 32 | 94.8 | 96 |
| 44k1_stereo_64k | sandman 10.0 s | 7.36 / 33 | 65.6 | 6.18 / 39 | 64.2 | 64 |
| 44k1_stereo_128k | sandman 10.0 s | 5.90 / 41 | 128.8 | 5.54 / 43 | 126.9 | 128 |
| 44k1_stereo_160k | sandman 10.0 s | 5.53 / 43 | 160.4 | 5.47 / 44 | 158.3 | 160 |
| 44k1_stereo_192k | sandman 10.0 s | 5.24 / 46 | 192.0 | 5.47 / 44 | 189.6 | 192 |
| 44k1_stereo_256k | sandman 10.0 s | 4.88 / 49 | 255.2 | 4.87 / 49 | 244.6 | 256 |
| 48k_stereo_16k | sandman 10.0 s | 10.42 / 23 | 22.0 | 7.12 / 34 | 17.5 | 16 |
| 48k_stereo_24k | sandman 10.0 s | 8.92 / 27 | 26.4 | 6.86 / 35 | 25.0 | 24 |
| 48k_stereo_32k | sandman 10.0 s | 8.48 / 28 | 34.2 | 6.44 / 37 | 32.9 | 32 |
| 48k_stereo_40k | sandman 10.0 s | 7.98 / 30 | 42.1 | 6.25 / 38 | 40.8 | 40 |
| 48k_stereo_48k | sandman 10.0 s | 7.69 / 31 | 50.1 | 6.05 / 40 | 48.7 | 48 |
| 48k_stereo_56k | sandman 10.0 s | 7.55 / 32 | 58.0 | 5.92 / 41 | 56.4 | 56 |
| 48k_stereo_64k | sandman 10.0 s | 7.02 / 34 | 65.8 | 5.82 / 41 | 64.2 | 64 |
| 48k_stereo_96k | sandman 10.0 s | 6.01 / 40 | 97.5 | 5.43 / 44 | 95.6 | 96 |
| 48k_stereo_128k | sandman 10.0 s | 5.67 / 42 | 129.1 | 5.22 / 46 | 127.0 | 128 |
| 48k_stereo_160k | sandman 10.0 s | 5.31 / 45 | 160.7 | 5.08 / 47 | 158.4 | 160 |
| 48k_stereo_192k | sandman 10.0 s | 5.04 / 48 | 192.4 | 5.07 / 47 | 189.8 | 192 |
| 48k_stereo_256k | sandman 10.0 s | 4.71 / 51 | 255.6 | 4.95 / 48 | 252.6 | 256 |
| 48k_stereo_320k | sandman 10.0 s | 4.62 / 52 | 318.8 | 4.65 / 52 | 264.1 | 320 |

</details>

<details>
<summary>Memory and footprint</summary>

* **Encoder state** (internal heap taken by `faac_encoder_open`, `FAAC_MAX_CHANNELS` 2): 56 KB LC mono, 89 KB LC stereo, 116 KB HE-AAC v1
  stereo. It is the same at every bitrate and sample rate. This is 14 to 42 KB less than the libfaac of the previous run (70 / 103 / 158 KB). Static RAM of
  the library is 20.5 KB `.bss` + 1 KB `.data`. The PCM input stays in PSRAM on the S3, so the state is the whole internal-RAM cost.
* **Stack:** 8.9 KB (LC) and 13.7 KB (HE-AAC v1) of the 32 KB main task stack (the high-water mark of the firmware's own task, so a real task
  needs about that plus its own frames).
* **Flash:** 43.5 KB for the libfaac archive (`.text` 33.4 KB + `.rodata` 9.1 KB), including HE-AAC v1; the C6 build is 50.7 KB and the classic
  ESP32 43.2 KB.
* **Open cost:** `faac_encoder_open` is 25 to 43 k cycles for LC (about 0.2 ms) and 0.62 M cycles for HE-AAC v1 (2.6 ms), after the one-time
  table setup, so a short-lived encoder instance is cheap.
* Compared with the decoders, encoding LC stereo takes 89 KB of RAM, more than libhelix (about 80 KB total) and less than FAAD3 (about
  139 KB) or FAAD2 (about 175 KB); HE-AAC v1 stereo takes 116 KB. The flash is in the FAAD3 range (55 KB) and below FAAD2.

</details>

<details>
<summary>Accuracy against the target bitrate</summary>

The target is the scenario's total kbps; libfaac gets it per channel. Actual kbps is the output size over the duration the stream covers
(priming and flush frames included); over the 51 runs the mean ratio is 1.03x (LC) and 1.00x (HE).

* LC: within 0 to 5% over the target from 24 kbps up on mono and above 48 kbps stereo, **but 11% over at 32k_stereo_16k (17.7 vs 16) and 37%
  over at 48k_stereo_16k (22.0 vs 16)**, 10% over at 48k_stereo_24k and 7% over at 48k_stereo_32k. The 16 kbps stereo scenarios (8 kbps per channel)
  sit on a libfaac quality floor: the resolved bit rate is 8000 per channel, the bandwidth is 9.5 to 9.75 kHz, and the stream stays above the target.
* LC mono comes out under on the 24 and 32 kbps rows (22.7 vs 24 and 29.4 vs 32, 5 to 8%); the speech clips have quiet passages.
* HE-AAC v1: 0.99 to 1.08 up to 192 kbps, 0.96 at 44.1 kHz/256 kbps, and **0.83 at 48k_stereo_320k (264 vs 320 kbps)**: the resolved per-channel
  rate reported by the device is 144 kbps there (160 requested), a clamp inside libfaac, so HE-AAC v1 cannot be pushed to 320 kbps at 48 kHz.

</details>

<details>
<summary>Correctness checks and root causes of the limits</summary>

* All 51 returned ADTS streams decode with ffmpeg at the expected profile (LC or HE-AAC v1), rate and channels, and the device CRC32, byte count
  and frame count matched the returned stream in every case.
* SNR against the source is not a quality metric: the decode is offset by the codec delay and the HE-AAC output is resampled; it only shows that the
  stream is not garbage. No MOS was computed.

**Causes of the outliers.** These carry over from the previous run's investigation, which traced each to the libfaac source and reproduced the
overshoot with the host `faac`; the source at `171671bb` still has `HE_MIN_SAMPLE_RATE` 32000 and the per-channel bit-rate clamp, and the measured rows
are unchanged, but the host reproduction was not repeated.

* **HE-AAC v1 is rejected on 16/24 kHz mono (5 rows).** `libfaac/frame.c` returns an error for an explicit HE-AAC v1 request below 32 kHz (core too
  narrow for SBR). The tool skips HE below 32 kHz and reports those rows as not applicable.
* **HE-AAC v1 at 320 kbps lands at 264 kbps (48 kHz).** After the HE resolution halves the core rate to 24 kHz, `frame.c` clamps the bit rate to the AAC
  limit of 6144 bits per frame and channel: 144 kbps per channel, 288 kbps stereo. The clamp is silent at `open()`; the encoder info reports the resolved
  rate, which the tool flags as "clamped".
* **LC overshoots at 8 kbps per channel.** It is libfaac's low-rate LC quality floor, not a device effect; with `--object-type auto` libfaac picks HE-AAC at
  that rate and lands on target.
* **Mono speech 5 to 8% under target:** rate-control convergence and quiet passages.

</details>

<details>
<summary>Encoder caveats</summary>

* One clip per scenario, and every stereo row uses the same music clip (`sandman`, first 10 s), so content effects (e.g. the 16 kbps overshoot) are not
  separated from rate effects; the mono rows use two speech clips of 8.6 s. Cost varies with content, so treat the xRT as a representative point, not a worst
  case. Run the full corpus (without `--gate`) for ranges.
* Single core, the encode call only, with the library's `double` code software-emulated on all three chips; changing that or the compiler flags would move
  these numbers. The build uses the ESP-IDF defaults with no tuning, and `sbr-decimation` 1.
* `FAAC_MAX_CHANNELS` is 2, so the 5.1 scenarios are skipped.
* No quality measurement was made; the bitrate accuracy is the only rate/quality statement.
* Tested with libfaac `171671bb` (`../faac`, `stack-tests`), ESP-IDF v5.5.5, GCC 14.2.0, ffmpeg 7.1.5.

</details>

### Results: ESP32-C6 @ 160 MHz

Gate list, one clip per scenario, `--seconds` trimmed to what fits the chip's internal RAM (0.5 to 3 s). The C6 has no FPU, so all of the encoder's
float arithmetic runs in software.

| Profile | Encodes | Mean xRT | Min xRT | MHz for 1x RT (mean / worst) | Mean actual/target kbps | Encoder heap | Stack used | ffmpeg decodes |
|---|---|---|---|---|---|---|---|---|
| HE-AAC v1 | 23 | 0.25 | 0.22 | 651 / 737 | 1.06x | 117 KB | 13.5 KB | 23/23 |
| LC | 28 | 0.86 | 0.38 | 281 / 421 | 1.10x | 89 KB | 8.5 KB | 28/28 |

* **Mono LC is real time (1.9 to 2.9x); stereo is not:** LC stereo runs at 0.38 to 0.82x and HE-AAC v1 stereo at 0.22 to 0.35x, so the C6 cannot encode
  44.1/48 kHz stereo live; it needs 195 to 420 MHz of CPU for LC and 461 to 736 MHz for HE-AAC.
* All 51 encodes completed and every stream decoded with ffmpeg. State sizes equal the S3's: 56 / 89 KB LC and 116 KB HE-AAC v1 stereo.

<details>
<summary>By scenario (all 28 scenarios)</summary>

| Scenario | Source | LC xRT / MHz | LC kbps | HE xRT / MHz | HE kbps | Target |
|---|---|---|---|---|---|---|
| 16k_mono_20k | R_01_CLIP_MK 3.1 s | 2.92 / 55 | 24.2 | n/a | - | 20 |
| 16k_mono_24k | R_01_CLIP_MK 3.1 s | 2.78 / 58 | 27.4 | n/a | - | 24 |
| 16k_mono_voip_24k | C_01_CHOP_FA 3.1 s | 2.93 / 55 | 29.0 | n/a | - | 24 |
| 24k_mono_28k | R_01_CLIP_MK 2.1 s | 1.87 / 85 | 38.0 | n/a | - | 28 |
| 24k_mono_32k | R_01_CLIP_MK 2.1 s | 1.86 / 86 | 38.7 | n/a | - | 32 |
| 32k_stereo_16k | sandman 0.8 s | 0.82 / 195 | 21.0 | 0.35 / 462 | 25.7 | 16 |
| 32k_stereo_48k | sandman 0.8 s | 0.67 / 238 | 50.7 | 0.33 / 486 | 58.3 | 48 |
| 32k_stereo_64k | sandman 0.8 s | 0.63 / 255 | 67.2 | 0.33 / 490 | 70.0 | 64 |
| 32k_stereo_80k | sandman 0.8 s | 0.59 / 270 | 83.0 | 0.32 / 494 | 82.7 | 80 |
| 32k_stereo_96k | sandman 0.8 s | 0.57 / 279 | 98.6 | 0.32 / 495 | 96.2 | 96 |
| 44k1_stereo_64k | sandman 0.6 s | 0.52 / 308 | 67.1 | 0.24 / 658 | 71.8 | 64 |
| 44k1_stereo_128k | sandman 0.6 s | 0.44 / 361 | 127.8 | 0.24 / 677 | 119.9 | 128 |
| 44k1_stereo_160k | sandman 0.6 s | 0.43 / 373 | 159.1 | 0.24 / 676 | 142.8 | 160 |
| 44k1_stereo_192k | sandman 0.6 s | 0.42 / 384 | 189.4 | 0.24 / 676 | 164.6 | 192 |
| 44k1_stereo_256k | sandman 0.6 s | 0.40 / 400 | 247.1 | 0.23 / 685 | 200.5 | 256 |
| 48k_stereo_16k | sandman 0.5 s | 0.60 / 268 | 25.5 | 0.24 / 668 | 25.0 | 16 |
| 48k_stereo_24k | sandman 0.5 s | 0.55 / 292 | 30.1 | 0.24 / 671 | 29.1 | 24 |
| 48k_stereo_32k | sandman 0.5 s | 0.54 / 296 | 36.2 | 0.23 / 683 | 38.6 | 32 |
| 48k_stereo_40k | sandman 0.5 s | 0.53 / 303 | 43.6 | 0.23 / 691 | 48.1 | 40 |
| 48k_stereo_48k | sandman 0.5 s | 0.52 / 308 | 51.3 | 0.23 / 697 | 56.8 | 48 |
| 48k_stereo_56k | sandman 0.5 s | 0.51 / 311 | 58.6 | 0.23 / 701 | 63.5 | 56 |
| 48k_stereo_64k | sandman 0.5 s | 0.49 / 324 | 66.5 | 0.23 / 705 | 70.7 | 64 |
| 48k_stereo_96k | sandman 0.5 s | 0.44 / 364 | 97.9 | 0.22 / 718 | 99.5 | 96 |
| 48k_stereo_128k | sandman 0.5 s | 0.43 / 376 | 126.8 | 0.22 / 724 | 118.2 | 128 |
| 48k_stereo_160k | sandman 0.5 s | 0.41 / 388 | 157.8 | 0.22 / 727 | 143.3 | 160 |
| 48k_stereo_192k | sandman 0.5 s | 0.40 / 399 | 187.5 | 0.22 / 728 | 165.2 | 192 |
| 48k_stereo_256k | sandman 0.5 s | 0.39 / 415 | 245.2 | 0.22 / 728 | 204.3 | 256 |
| 48k_stereo_320k | sandman 0.5 s | 0.38 / 421 | 300.9 | 0.22 / 737 | 217.4 | 320 |

</details>

### Results: classic ESP32 @ 240 MHz

Gate list, one clip per scenario, `--seconds` trimmed to what fits the internal RAM (0.3 to 2 s of PCM held in one contiguous block; no PSRAM).

| Profile | Encodes | Mean xRT | Min xRT | MHz for 1x RT (mean / worst) | Mean actual/target kbps | Encoder heap | Stack used | ffmpeg decodes |
|---|---|---|---|---|---|---|---|---|
| LC | 27 | 9.35 | 4.02 | 37 / 60 | 1.13x | 90 KB | 8.6 KB | 27/27 |

Not ok: he out of memory: 23, lc out of memory: 1

* **LC runs in real time on all rows that fit:** mono at 18 to 29x, stereo at 4.0 to 10.3x (23 to 59 MHz per real-time stream).
* **HE-AAC v1 stereo does not run:** all 23 HE rows fail with "out of memory", even with the 116 KB state of the current libfaac (the previous 158 KB state also
  failed). The 116 KB state plus a PCM block does not fit the 108 KB largest contiguous block plus its neighbours.
* One LC row (320 kbps, 48 kHz stereo) failed with out of memory too.

<details>
<summary>By scenario (rows that ran)</summary>

| Scenario | Source | LC xRT / MHz | LC kbps | HE xRT / MHz | HE kbps | Target |
|---|---|---|---|---|---|---|
| 16k_mono_20k | R_01_CLIP_MK 2.0 s | 29.22 / 8 | 25.3 | n/a | - | 20 |
| 16k_mono_24k | R_01_CLIP_MK 2.0 s | 27.27 / 9 | 28.3 | n/a | - | 24 |
| 16k_mono_voip_24k | C_01_CHOP_FA 2.0 s | 29.01 / 8 | 31.5 | n/a | - | 24 |
| 24k_mono_28k | R_01_CLIP_MK 1.3 s | 17.85 / 13 | 32.0 | n/a | - | 28 |
| 24k_mono_32k | R_01_CLIP_MK 1.3 s | 17.85 / 13 | 32.0 | n/a | - | 32 |
| 32k_stereo_16k | sandman 0.5 s | 10.27 / 23 | 24.7 | n/a | - | 16 |
| 32k_stereo_48k | sandman 0.5 s | 7.86 / 31 | 53.1 | n/a | - | 48 |
| 32k_stereo_64k | sandman 0.5 s | 7.01 / 34 | 70.8 | n/a | - | 64 |
| 32k_stereo_80k | sandman 0.5 s | 6.52 / 37 | 86.8 | n/a | - | 80 |
| 32k_stereo_96k | sandman 0.5 s | 6.20 / 39 | 102.5 | n/a | - | 96 |
| 44k1_stereo_64k | sandman 0.4 s | 6.10 / 39 | 68.6 | n/a | - | 64 |
| 44k1_stereo_128k | sandman 0.4 s | 4.92 / 49 | 129.0 | n/a | - | 128 |
| 44k1_stereo_160k | sandman 0.4 s | 4.64 / 52 | 159.6 | n/a | - | 160 |
| 44k1_stereo_192k | sandman 0.4 s | 4.46 / 54 | 189.1 | n/a | - | 192 |
| 44k1_stereo_256k | sandman 0.4 s | 4.19 / 57 | 243.6 | n/a | - | 256 |
| 48k_stereo_16k | sandman 0.3 s | 7.87 / 30 | 28.1 | n/a | - | 16 |
| 48k_stereo_24k | sandman 0.3 s | 7.06 / 34 | 33.4 | n/a | - | 24 |
| 48k_stereo_32k | sandman 0.3 s | 6.81 / 35 | 39.4 | n/a | - | 32 |
| 48k_stereo_40k | sandman 0.3 s | 6.47 / 37 | 46.1 | n/a | - | 40 |
| 48k_stereo_48k | sandman 0.3 s | 6.35 / 38 | 52.9 | n/a | - | 48 |
| 48k_stereo_56k | sandman 0.3 s | 6.25 / 38 | 59.9 | n/a | - | 56 |
| 48k_stereo_64k | sandman 0.3 s | 5.81 / 41 | 68.1 | n/a | - | 64 |
| 48k_stereo_96k | sandman 0.3 s | 4.95 / 49 | 101.5 | n/a | - | 96 |
| 48k_stereo_128k | sandman 0.3 s | 4.71 / 51 | 128.6 | n/a | - | 128 |
| 48k_stereo_160k | sandman 0.3 s | 4.44 / 54 | 159.2 | n/a | - | 160 |
| 48k_stereo_192k | sandman 0.3 s | 4.27 / 56 | 188.9 | n/a | - | 192 |
| 48k_stereo_256k | sandman 0.3 s | 4.02 / 60 | 243.0 | n/a | - | 256 |

</details>

## Classic ESP32 limits (no PSRAM)

What the original ESP32 (ESP32-D0WDQ6 rev v1.0, 240 MHz, no PSRAM) can run. The binding
constraint is internal RAM: 257,224 B are free after boot, but DRAM is split into regions, so the largest contiguous
block is only 110,592 B (108 KB). Wi-Fi, Bluetooth and the rest of an application come out of the same pool and were
not measured.

* **Decoders:** all three decode every gate bitstream (196 per decoder, streamed, clips up to 400 KB) in real time: stereo LC 4.9 to 24.5x,
  HE-AAC 2.0 to 5.5x; heap 79 KB (libhelix), 116 KB (FAAD2, plus 56 KB of stack), 97 KB (FAAD3, 6 KB of stack), the same as on the S3.
  Output is bit-identical to the S3's on all 588 decodes. Streaming (the host feeds the clip in blocks, see Method) removed the old
  stored-clip ceiling (39 of 196 bitstreams per decoder); it was validated before the sweep: PCM CRCs equal to the S3's on all 64
  clips streamed, and cycles within 0.05% of a resident run of the same firmware.
* **Encoder, LC:** 56 KB (mono) and 89 KB (stereo) of state; 4.0 to 10.3x real time for stereo, 18 to 29x for mono, so the CPU is
  not the limit at any bitrate up to the format ceiling. The PCM is held in one contiguous block, so a clip is at most about 2 s of
  16 kHz mono or 0.3 to 0.5 s of 48 kHz stereo (`--seconds` is trimmed to fit).
* **Encoder, HE-AAC v1:** does not run on this chip with the current libfaac. The state is 116 KB (it was 158 KB and 161 KB
  before) and every one of the 23 stereo HE rows failed with out of memory: the largest contiguous block is 108 KB, so the state
  cannot be allocated as one piece or leaves no room for any PCM. The state is smaller than before but not yet small enough.
* **Whole-clip benchmarking is the tighter limit** for the encoder: the firmware holds the PCM in one contiguous block. A streaming
  encoder input (the same idea as the decoder's) would remove it; it has not been built.

The earlier probing (longest encoder clip by bisection, state sizes of a reduced-memory libfaac branch) was against libfaac `3a8d8d5f`
and is not carried over; the reduced-memory work appears to be in the current libfaac (116 KB HE-AAC v1 stereo state, 89 KB LC), but a
bisection of the longest clip was not repeated.

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
* **Resident vs streaming clips.** The S3 holds the clip in PSRAM. On the chips without PSRAM (`--stream auto`) the host can instead
  feed the clip in blocks: the device keeps a 16 KB window, asks for the next block (`AACQ`) between decode calls, and streams the
  PCM of the first pass back as it decodes (`AACD`); the timed region is unchanged and the window is excluded from the heap figures
  (protocol in `scripts/esp32/firmware_dec/README.md`). On the same firmware, streamed and resident runs of the same 4 FAAD3 clips
  differ by 0.04% in cycles.
* **Build-to-build layout effect on the classic ESP32:** the same FAAD3 LC clips took about 12% fewer cycles in a later firmware build
  than in the one the stored-clip run used (resident or streamed, both in the later build agree), with the decoder sources unchanged;
  code placement in the flash cache is the likely cause and was not isolated. Compare classic ESP32 numbers within one build.

</details>

<details>
<summary>Codec builds</summary>

| | libhelix-aac | FAAD2 | FAAD3 |
|---|---|---|---|
| Source | `../codec-helix/src/libhelix-aac` (C API, not the C++ wrapper) | `../faad2/libfaad` | `libfaad/` + `common/` of faac `origin/stack-tests`, exported read-only by `fetch_faad3.sh` |
| Arithmetic | fixed point | fixed point | float (also uses `double` in places; not audited) |
| Config | SBR on (`HELIX_FEATURE_AUDIO_CODEC_AAC_SBR`), generic-C `MULSHIFT32` (no Xtensa/RISC-V asm) | `FIXED_POINT`, `LC_ONLY_DECODER`, SBR on, `dontUpSampleImplicitSBR=1`, 16-bit output | `faad.h` API (`faad_decoder_open`, split allocations), ADTS, 16-bit output, SBR on, PS on (`CODEC_PS=1`; the `faad3` variant in `esp32_dec_bench.py`), `MAX_CHANNELS=2`, hand-written `config.h` |
| State | allocated from internal RAM | `malloc` (internal RAM) | several `malloc`ed blocks (internal RAM) |
| Notes | | fixed-point + `SBR_LOW_POWER` does not compile upstream, so SBR builds keep the PS code (never exercised by FAAC's HE v1 streams; on HE v2 streams it stops responding, see the HE-AAC v2 section); needs ~57 KB of stack | WIP branch, so numbers can move |
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
| libhelix-aac | `nschimme/codec-helix` `a84de91` (`main`), sources used from `src/libhelix-aac` |
| FAAD2 | `2.11.4-1-g864ccb5` (`../faad2`, master) |
| FAAD3 | faac `origin/stack-tests` @ `171671bb` (WIP, exported by `fetch_faad3.sh`; includes the split allocations of [nschimme/faac#36](https://github.com/nschimme/faac/pull/36)) |
| libfaac (encoder) | `../faac` `171671bb` (`stack-tests`) |
| Bitstreams | FAAC 2.1.0 (`3d077533`), LC and HE-AAC v1, from `comparison_results.json` |
| faac-benchmark | `53e2ba7` plus the uncommitted `scripts/esp32/` work |

Boards: ESP32-S3 (QFN56 rev v0.2, 8 MB octal PSRAM, 240 MHz), ESP32-C6 (QFN40 rev v0.2,
160 MHz), ESP32 WROOM (ESP32-D0WDQ6 rev v1.0, 240 MHz, CP2102 bridge).

</details>

<details>
<summary>Decoder caveats</summary>

* One clip is decoded at a time with the clip held in memory, so the numbers are decode cost only.
  The S3 input sits in 80 MHz octal PSRAM, which can cost a little through the cache; decoder state
  and the PCM scratch buffer are in internal RAM (except in the `-psram` runs).
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
  plus an entry in the `CODECS` table of `esp32_dec_bench.py`. The FAAC encoder is a separate tool (see above).

</details>
