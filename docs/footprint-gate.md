# Footprint gate

## Why a section sum, not the file size

`lib_size` (whole-file `getsize`) was compared and printed but never gated, and
it could not have been: whole-file size moves with symbol tables, `.eh_frame`,
build IDs and section padding, none of which are code. `.text + .rodata` of the
release shared library does not move for those reasons. `.rodata` is in the sum
because the next footprint regression is as likely to be a lookup table as a
loop.

Per-object `.text` is recorded alongside it, ungated. It is what turns "the
library grew" into "`frame.c.o` grew" without a bisection.

## How the threshold was chosen

Replayed the 30 commits ending at `6d191ca7`, building each at
`--buildtype=release` with one toolchain, and recorded `.text + .rodata`:

| Δ bytes | commit |
| ---: | :--- |
| +13433 | add HE-AAC v1 (SBR) encoding support (#143) |
| +7096 | relicense: reimplement filtbank under LGPL 2.1 (#148) |
| +5048 | standardize libfaac on single-precision floating point (#152) |
| **+3504** | **tns: screen frames on the temporal envelope** (`f94a81a8`) |
| +3416 | relicense: reimplement tns under LGPL 2.1 (#149) |
| +1556 | harden input parsing and guard against allocation failures |
| +1380 | fft: radix-4 DIF as the sole FFT |
| +1358 | api: replace legacy faacEnc* surface |
| −3160 | tns: take the attack gate's envelope from psy (`0c91069e`) |
| −6033 | relicense: reimplement channels and bitstream (#146) |

Everything else moved by less than 100 bytes.

Routine work tops out at **+1556**. Notable work starts at **+3416**. The band
between them is empty, so `FOOTPRINT_FAIL_BYTES = 2048` is not a guess — it is
the middle of a gap that 30 commits of history never landed in. The 0.5%
companion test only starts binding above a ~410 KB library; today the byte floor
governs.

## What the calibration did *not* achieve

The design bar was "zero false trips while still tripping on the known
regression." That is unachievable, and the table says why: four legitimate
commits are **larger** than the regression. `f94a81a8` was not anomalous in
size — 3504 bytes for an added analysis pass is unremarkable. It was anomalous
in *value*: the same statistic was already sitting in `psydata`, and `0c91069e`
later recovered 3160 of those bytes by reading it instead of recomputing it.

Magnitude cannot distinguish growth that buys something from growth that does
not. So the gate does not try. It fails on growth past the threshold and asks
the author to confirm, via `--footprint-allow BYTES`, with the intended cost
stated in the PR. Expect it to fire roughly once every six commits, which is
about how often this project actually changes size.

## Toolchain identity

`toolchain_fp` (compiler version, target triple, buildtype, LTO, optimization,
`default_library`, `tuning`) is recorded in every results file, and the gate
**skips** when base and candidate disagree. A compiler or runner-image bump
changes both size and timing with zero source change; without this the stale
baseline gets reused across the bump and the diff is charged to the PR.

A skipped gate prints as loudly as a failing one (`⏭️` with the reason). A gate
that goes quiet when it cannot be evaluated is indistinguishable from a gate
that passed, which is the failure mode this whole file exists to prevent.
