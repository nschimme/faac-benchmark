# Benchmarking Guide

## Corpora

A **corpus** is one directory of reference WAVs in ONE fixed format, declared in
`config.py` (`CORPORA`). It is the only place a sample rate or channel count is
written down; scenarios point at a corpus and inherit both. `setup_datasets.py`
builds each one, and does so per-corpus — a corpus added later is built on the
next run without re-downloading everything (`--force` rebuilds all).

| Corpus | Directory | Format | Built from |
| :--- | :--- | :--- | :--- |
| `speech_clean_16k` | `data/external/speech_clean_16k/` | 16 kHz mono | TCD-VoIP clean `ref/` recordings |
| `speech_clean_24k` | `data/external/speech_clean_24k/` | 24 kHz mono | the same, at 24 kHz |
| `speech_voip_16k` | `data/external/speech/` | 16 kHz mono | TCD-VoIP degraded test set |
| `audio_32k` | `data/external/audio_32k/` | 32 kHz stereo | downsampled from `audio` |
| `audio_44k1` | `data/external/audio_44k1/` | 44.1 kHz stereo | downsampled from `audio` |
| `audio_48k` | `data/external/audio/` | 48 kHz stereo | PMLT2014 + SoundExpert |

A corpus may declare `max_clips`/`strata`, a deterministic cap applied by
`utils.select_corpus_clips`: clips are taken round-robin across the strata its
regex matches (degradation type and talker, for the speech sets) so no stratum
is dropped. The 400-clip TCD-VoIP set is 5 degradation types x ~20 conditions x
4 talkers describing one configuration, and capping it is what pays for the
extra rate families. `--gate` bypasses the cap: it selects from its own curated
list.

The **clean** speech corpus carries the quality ladder. The degraded VoIP set
is kept as a single spot-check scenario because ViSQOL scores the encode
against the degraded file — the chop/clip/echo/noise sits in the *reference*
and widens the variance a real regression has to clear — while still covering
the noisy-input quantizer/PNS path and VoIP transcoding.

## Scenarios

Scenarios are defined in `config.py` (`SCENARIOS`). Each fixes a **mode**
(which metric engine), a **corpus** (which content, and therefore the sample
rate and channel count), a **bitrate**, and a quality threshold. The bitrate is
part of the scenario's identity, so studying a different bitrate means adding a
scenario, not overriding `-b`. `rate`, `channels` and `visqol_rate` are derived
from the corpus and the mode — never set them by hand.

| Scenario | Corpus | Format | Bitrate | Per-channel | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `16k_mono_20k` | speech_clean_16k | 16k mono | 20k | 20k | telephony; bottom of this format's usable window |
| `16k_mono_24k` | speech_clean_16k | 16k mono | 24k | 24k | top of the same window |
| `16k_mono_voip_24k` | speech_voip_16k | 16k mono | 24k | 24k | VoIP-degraded spot check, paired with the row above |
| `24k_mono_28k` | speech_clean_24k | 24k mono | 28k | 28k | wideband speech |
| `24k_mono_32k` | speech_clean_24k | 24k mono | 32k | 32k | wideband speech |
| `32k_stereo_16k` | audio_32k | 32k stereo | 16k | **8k/ch** | HE-AAC's design floor |
| `32k_stereo_48k` | audio_32k | 32k stereo | 48k | 24k/ch | low-rate **LC** (48 kHz switches to HE here) |
| `32k_stereo_96k` | audio_32k | 32k stereo | 96k | 48k/ch | broadcast-style |
| `44k1_stereo_64k` | audio_44k1 | 44.1k stereo | 64k | 32k/ch | 44.1k SFB tables, low rate |
| `44k1_stereo_128k` | audio_44k1 | 44.1k stereo | 128k | 64k/ch | the common real-world config |
| `44k1_stereo_192k` | audio_44k1 | 44.1k stereo | 192k | 96k/ch | near transparency |
| `48k_stereo_24k` | audio_48k | 48k stereo | 24k | 12k/ch | HE-AAC Floor |
| `48k_stereo_32k` | audio_48k | 48k stereo | 32k | 16k/ch | |
| `48k_stereo_40k` | audio_48k | 48k stereo | 40k | 20k/ch | low-rate music |
| `48k_stereo_48k` | audio_48k | 48k stereo | 48k | 24k/ch | low-rate music |
| `48k_stereo_56k` | audio_48k | 48k stereo | 56k | 28k/ch | HE-AAC Ceiling |
| `48k_stereo_64k` | audio_48k | 48k stereo | 64k | 32k/ch | LC-AAC Low |
| `48k_stereo_96k` | audio_48k | 48k stereo | 96k | 48k/ch | |
| `48k_stereo_128k` | audio_48k | 48k stereo | 128k | 64k/ch | LC-AAC Common |
| `48k_stereo_160k` | audio_48k | 48k stereo | 160k | 80k/ch | |
| `48k_stereo_192k` | audio_48k | 48k stereo | 192k | 96k/ch | |
| `48k_stereo_256k` | audio_48k | 48k stereo | 256k | 128k/ch | transparency |
| `48k_stereo_320k` | audio_48k | 48k stereo | 320k | 160k/ch | top of the format's usable range |

`48k_stereo_40k` / `48k_stereo_48k` are named **by rate, not codec**. While HE-AAC is not
auto-engaged in faac they run as pure LC (valid low-rate LC tests); once faac's
auto-mode picks HE at those per-channel rates, the same scenario becomes the
HE-vs-LC comparison at the bitrates where HE-AAC v1 is designed to win — no
benchmark change required.

### Every target bitrate must be reachable

`python3 scripts/validate_scenarios.py` encodes each scenario's gate clips and
reports achieved-vs-target, failing anything outside ±15%. **Run it before
adding or retuning a scenario.** A target the format cannot carry becomes a
permanent accuracy deficit reported in every run that no code change can fix:
the retired `16k_mono_40k` asked 16 kHz mono for 40 kbps and got 32.2 (-20%),
because the format saturates well below it.

The mono families turn out to track a target only inside a **narrow window**,
and they fail at *both* ends (see `config.py` for the full curves):

| 16 kHz mono | 12k | 16k | 20k | 24k | 28k | 32k |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| achieved | +24.5% | +11.1% | **+4.1%** | **-4.7%** | -11.8% | -17.4% |

Below the window the encoder simply will not emit fewer bits on this content
(a floor near 17 kbps), so a smaller target overshoots; above it, the content
is already transparent and the encoder will not spend more. Both mono ladders
sit at the two ends of their window (20k/24k and 28k/32k) — that, not a
conventional-looking round number, is what makes the rung measurable.

Note the script's own caveat: on a stock LC-only faac the HE-AAC-targeted
stereo scenarios (24-56 kbps) read as large overshoots, because that build
never engages HE-AAC. On a build with AUTO object-type resolution they land
within tolerance (measured +9.4% at 24 kbps). That is a property of the binary,
not of the scenario — check the build before retargeting there.

**Every bitrate figure in this repo is quoted against one reference build**
(`faac` master @ `8aebaa31`, recorded at the top of `config.py`). Reachability
is as much a property of the encoder as of the format, so a number is only
interpretable next to the build that produced it — quote the build whenever you
record a new one.

### What each family actually exercises

Verified with `ffprobe` on a `--gate` run against the reference build:

| Family | Object type across the ladder |
| :--- | :--- |
| `16k_mono`, `24k_mono` | LC throughout |
| `32k_stereo` | HE-AAC at 16k (pinned), LC at 48k and 96k |
| `44k1_stereo` | HE-AAC at 64k, LC at 128k and 192k |
| `48k_stereo` | HE-AAC from 24k to 96k, LC from 128k up |

Note this is a **property of the build, not of the bitrate**: object-type AUTO
resolution moves. On the reference build HE-AAC reaches up to 96 kbps
(48 kbps/channel); on the `bandwidth-curve-smoothing` branch the same clip at
the same bitrate switches to LC from 56k up. Re-check with `ffprobe` rather
than assuming — and note the pre-existing "HE-AAC Ceiling" label on
`48k_stereo_56k` in the table above predates all of this and is inaccurate on
both builds.

The contrast is the point of the 32 kHz family: at 24 kbps/channel the 48 kHz
ladder answers with HE-AAC, so the low-rate **LC** path is only measured at all
because a lower sample rate reaches those bitrates without triggering the
switch.

### Scenarios blocked on an encoder fix

The reachability rule assumes the *scenario* is what might be wrong. Sometimes
it is the encoder, and a fix is already in flight — the scenario is right and
should be kept. `scripts/validate_scenarios.py` carries a `PENDING_UPSTREAM`
map for exactly that: those scenarios are reported with their deviation but do
not fail the run, and each entry names the fix.

Two of the current scenarios sit at the ends of the range the reference build
gets wrong, both addressed by [nschimme/faac#451][pr451]:

| Scenario | reference build | with #451 |
| :--- | :---: | :---: |
| `32k_stereo_16k` (8k/ch) | +28.5%, LC | **+11.9%, HE-AAC** |
| `48k_stereo_320k` | -7.6% (saturated) | **+2.2%** |

At 8 kbps/channel AUTO refuses HE-AAC and falls back to LC, which cannot reach
that rate; faac has no HE-AAC v2 to escalate to either. At the top end the `-b`
ceiling divides a per-channel bound by the channel count, so 320k, 384k and
448k all collapse to the *same* 295.7 kbps stream — 320k is nominally within
tolerance on the reference build but is measuring the bug. With #451 the
ceiling opens up (384k -> +1.2%, 448k -> -6.8%), making 448k a viable rung too.

Delete a `PENDING_UPSTREAM` entry once its fix lands. If the scenario still
deviates afterwards, that is a real finding and should fail.

[pr451]: https://github.com/nschimme/faac/pull/451

### Rate families

Scenarios sharing a rate/channel format form a **family** (`16k_mono`,
`24k_mono`, `32k_stereo`, `44k1_stereo`, `48k_stereo`; order in
`config.FAMILY_ORDER`). Reports plot MOS against bitrate, which is only
meaningful within one family — two scenarios at 48 kbps and different sample
rates are not two points on one curve — so the leaderboard emits one charted
section per family, and the A/B report groups its scenario table by family and
adds a per-family MOS Δ rollup to the summary.

## Filtering and sampling

* `--scenarios a,b` — run only these scenarios. A **family** name works too
  (`--scenarios 44k1_stereo` runs every 44.1 kHz scenario).
* `--include-tests`, `--exclude-tests` — filename globs.
* `--coverage N` — deterministic N% stride sample of each scenario.
* `--gate` — the fixed fast subset (`config.GATE_CLIPS`); ignores `--coverage`.

## A/B mode

`--compare "TAG:--args" ...` encodes the corpus once per arg-set and prints a
ranked per-clip diff (first run is the baseline). Example: `lc` vs `he`.

## Sweeps

`--sweep "KEY=v1,v2,..."` runs one tagged encode per value, each auto-diffed
against the first value:

* `KEY` starting with `-` is a **faac CLI flag** (e.g. `--pns=0,2,4`).
* otherwise `KEY` is an **environment variable** (for builds with tuning hooks).
* `-b` / `--bitrate` / `-q` are **rejected** — use a scenario instead.

## Reproducibility: provenance & decode validation

* Each encoded clip records a **provenance hash** of `(faac binary, libfaac.so,
  faac args, FAAC_* env, input file)`. Phase 2 refuses to reuse a cached MOS
  whose hash no longer matches — so a stale `.aac` can never be silently
  re-scored. Pass `--faac-bin`/`--lib-path` (run_benchmark does this
  automatically) to enable the check.
* Each encode is **decode-validated** with ffmpeg. ffmpeg exits 0 even on hard
  decoder errors (e.g. a corrupt SBR payload), so validation also treats any
  `-v error` stderr as a failure. Failures are recorded per clip
  (`decode_error`) and counted in the report. See [ci.md](ci.md) for
  `--strict-decode`.
