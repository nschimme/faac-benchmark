# DAB+ (960-sample) decoder conformance probe

Checks libfaad3's 960-sample AAC-LC, HE-AAC v1 and HE-AAC v2 decoding against
independent decoders. Stock fdk-aac cannot encode a 960-sample granule, so the
streams come from the Opendigitalradio fork (`build_odr_fdk.sh`).

- `enc.c` encodes a wav to length-prefixed raw access units plus the ASC
  (`<u32 asc length><asc>{<u32 length><access unit>}...`).
- `dec3.c` / `decfdk.c` / `dec2.c` decode that format with libfaad3, FDK and
  FAAD2. `loas.py` wraps it in LATM/LOAS so FFmpeg can decode it.
- `psm.c` compares the mid channel and stereo image of two stereo outputs
  (parametric stereo does not reproduce a decoder's side signal sample for
  sample); `snr.c` aligns two outputs by delay and prints the SNR.
- `run_matrix.sh` runs LC, HE v1 and HE v2 at 32 and 48 kHz, mono and stereo.

Reading the numbers:
- FDK is the reference. FFmpeg's 960 parametric stereo and FAAD2's parametric
  stereo both deviate from it; FAAD2 is not ground truth for PS.
- A stream that every decoder disagrees on (mono HE v1 at 32 kHz: all pairs at
  37-42 dB) is a property of the stream, not of one decoder.
- Expect 60-100 dB for the other configurations on the current libfaad.

The three vectors in faac's `tests/data/dab` are the first access units of
`lc_mono32`, `lc_st48` and `he2_st48`, with FDK's decoded output as reference.

Needs a C compiler, FFmpeg, FDK's decoder library (`libfdk-aac`), and a faac
build with the static library (`-Ddefault_library=both`).
