# scripts/core

Ad hoc A/B runners for LC-core investigation. Not wired into the main
benchmark; invoke directly with `.venv/bin/python`.

- `core24.py OUT.csv --kbps 44 --arms NAME=CMD ...` -- LC-at-24kHz core A/B: encodes
  24 kHz stereo (resampled from the corpus) clips as LC, decodes with ffmpeg,
  and scores against the 24 kHz reference (both upsampled to 48k, optionally
  low-passed via `--lp HZ`). `CMD` tokens support `{in} {out} {bps} {kbps}`
  placeholders and `ENV:K=V` prefix tokens for per-arm environment overrides.
- `runs.py FAACBIN OUT.csv --rates 48 --extra "..." --arms "A=ENV=1 ..." ...` --
  like `align/runc.py` plus stereo coherence (`ic`, and `ic_hi` restricted to
  content above 1.5 kHz) and an optional `--fdk` decode via `bin/fdkdec`.
- `an2.py DUMP...` -- FAAD_DUMP side-info accounting (section + scalefactor
  bits) for long CPE frames only.
- `an3.py DUMP...` -- like `an2.py` but covers both long and short frames,
  reported separately.
- `icref.py` -- stereo coherence error of FAAC/fdk/Apple HE-AAC at 48k
  (ffmpeg decode, cross-correlation aligned) across the corpus. Requires
  `FAAC_BIN=/path/to/faac`.
- `pat.py DUMP FRAMES` -- per-band codebook pattern printer for the given
  comma-separated frame numbers from a FAAD_DUMP file.
- `ident.sh NEWBIN REFBIN "objtype rate" ...` -- decoded-md5 identity check
  between two faac binaries across a couple of fixed clips.
