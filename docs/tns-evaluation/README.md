# Reproduce the short-window TNS comparison

`scripts/tns_evaluation.py` runs the selected 48 kHz stereo, 128 kb/s LC scenario through `compare_codecs.py`. It uses the ordinary benchmark encoder, decoder, MOS, stereo, attack, and reference-locked pre-echo scorers. On macOS, Apple AAC is included when `afconvert` is available; fdkaac is required. On other hosts Apple is explicitly recorded as unavailable.

## Prepare and run

Install the benchmark requirements and datasets using the repository setup instructions. Build two FAAC executables from the baseline and candidate revisions with matching build options. Keep each executable paired with its intended dynamic library; use wrapper executables if both builds need different libraries at runtime. Obtain `fdkaac` from the same pinned build for repeated runs.

The checked-in input manifest freezes the corpus clip names and SHA-256 hashes. The command refuses a changed, incomplete, or expanded corpus. It also records executable hashes, benchmark commit, host, input-manifest hash, command, AAC hashes, and all per-clip metric values.

```sh
python3 scripts/tns_evaluation.py \
  --baseline /path/to/faac-baseline \
  --candidate /path/to/faac-tns \
  --fdkaac /path/to/fdkaac \
  --data /path/to/faac-benchmark/data/external \
  --out /path/to/new-tns-evidence \
  --faac-version baseline tns-candidate
```

The output directory must be new or empty. It contains the raw benchmark result, the standard leaderboard report, the executed command, provenance, and `per-clip.csv`. Run it from any working directory; paths can be absolute. For a different set of inputs or scenario, make and review a new manifest rather than silently changing this one. The manifest identifies the 77 `audio_48k` inputs used by `48k_stereo_128k`; the scenario config supplies the 128 kb/s target and LC profile.

## Reading results

Each scored encoder/clip row retains MOS, achieved bitrate, stereo coherence error, attack-centroid measurements, and reference-locked pre-echo measurements and coverage. Missing encoder coverage or ineligible pre-echo events are N/A, never zero. Check `status`, `pre_echo_coverage`, and the scorer's error fields in the raw result before comparing coverage. Attack and pre-echo are independent diagnostic dimensions; keep them separate from MOS and stereo. Compare MOS at achieved bitrate and use a bitrate ladder/BD-rate when making bitrate-efficiency claims. This single scenario does not establish BD-rate or an overall quality win.

Apple AAC is a host-provided encoder, so its exact framework binary is not independently hashed. Its detected version, the host OS, command, and encoded-stream hashes remain in the result bundle. Identical source and configuration hashes do not guarantee identical Apple output across OS releases.

## Frozen evidence snapshot

`evidence-48k128/` is a compact, already measured baseline/candidate snapshot, included as an example of the output fields rather than a substitute for rerunning. It contains all 77 per-clip paired rows and input hashes. At nominal 128 kb/s, the broader recorded results showed pre-echo improvements on eig and fatboy, alongside MOS BD-rate costs from +4.878% to +18.078% across the four target curves. In the 77-clip corpus, 36 clips had eligible pre-echo events: 28 improved and 8 worsened; 41 were N/A. The eight-clip holdout had events in only three clips. These are separate measurements and do not reproduce the original ABX reports or establish an overall quality win. See `summary.md` for sources, exact scope, and limitations.
