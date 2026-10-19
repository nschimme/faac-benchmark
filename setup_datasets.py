"""
 * FAAC Benchmark Suite
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.

 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, write to the Free Software
 * Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
"""

import argparse
import glob
import json
import os
import urllib.request
import zipfile
import shutil
import wave
import re
import ffmpeg

DATASETS = {
    "PMLT2014": {
        "url": "https://github.com/nschimme/PMLT2014/archive/refs/tags/PMLT2014.zip",
        "name": "Public Multiformat Listening Test @ 96 kbps (July 2014)"
    },
    "TCD-VOIP": {
        "url": "https://github.com/nschimme/TCD-VOIP/archive/refs/tags/harte2015tcd.zip",
        "name": "TCD-VoIP (Sigmedia-VoIP) Listener Test Database"
    },
    "SoundExpert": {
        "url": "https://github.com/nschimme/SoundExpert/archive/refs/tags/SoundExpert.zip",
        "name": "SoundExpert Sound samples"
    },
    "IgorCPBLT2020": {
        "url": "https://github.com/nschimme/IgorCPBLT2020/archive/refs/tags/IgorCPBLT2020.zip",
        "name": "Igor's Personal Blind Test, multicodec ~192 kbps (2020)"
    },
    "Kamedo2PLT2012": {
        "url": "https://github.com/nschimme/Kamedo2PLT2012/archive/refs/tags/Kamedo2PLT2012.zip",
        "name": "Kamedo2 Personal Listening Test (2012)"
    },
    "FFmpeg51": {
        "url": "https://samples.mplayerhq.hu/A-codecs/lossless/6_Channel_ID.wav",
        "name": "FFmpeg 5.1 Surround Reference Sample",
    }
}

# Paths relative to script directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DATA_DIR = os.path.join(SCRIPT_DIR, "data", "external")
TEMP_DIR = os.path.join(SCRIPT_DIR, "data", "temp")


def download_and_extract(name, url):
    os.makedirs(TEMP_DIR, exist_ok=True)
    zip_path = os.path.join(TEMP_DIR, f"{name}.zip")

    # Download if missing or corrupted (GitHub sends a redirect; validate with zipfile).
    need_download = True
    if os.path.exists(zip_path):
        try:
            with zipfile.ZipFile(zip_path, 'r') as _zf:
                pass  # quick integrity check
            need_download = False
        except zipfile.BadZipFile:
            print(f"Cached {name}.zip is corrupt, re-downloading...")
            os.remove(zip_path)

    if need_download:
        print(f"Downloading {name}...")
        # Use urllib with explicit redirect following and streaming write to
        # handle GitHub's codeload redirects reliably.
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as response, open(zip_path, 'wb') as out:
            shutil.copyfileobj(response, out)
        # Validate what we got
        try:
            with zipfile.ZipFile(zip_path, 'r') as _zf:
                pass
        except zipfile.BadZipFile as e:
            os.remove(zip_path)
            raise RuntimeError(f"Downloaded {name}.zip is not a valid zip: {e}")

    print(f"Extracting {name}...")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(TEMP_DIR)


def get_info(wav_path):
    try:
        with wave.open(wav_path, 'rb') as f:
            frames = f.getnframes()
            rate = f.getframerate()
            channels = f.getnchannels()
            return frames / float(rate), channels
    except BaseException:
        return 0, 2


def get_info_ffprobe(path):
    """(duration, channels) for formats the wave module can't read (FLAC)."""
    try:
        probe = ffmpeg.probe(path)
        stream = next(st for st in probe["streams"] if st["codec_type"] == "audio")
        return float(probe["format"]["duration"]), int(stream["channels"])
    except BaseException:
        return 0, 2


def get_rate(wav_path):
    """Source sample rate, or 0 if unreadable."""
    try:
        with wave.open(wav_path, 'rb') as f:
            return f.getframerate()
    except BaseException:
        return 0


# swr with a long, steep filter. ffmpeg's defaults (filter_size 32, cutoff 0.97)
# droop ~0.7 dB at 20 kHz and ~4 dB near 22 kHz when going 44.1 -> 48 kHz, and
# image only 10-17 dB down above the source Nyquist; these settings are flat to
# 21.5 kHz with the image ~95 dB down. Same engine, so no extra dependency.
HQ_SWR = dict(filter_size=256, phase_shift=16, cutoff=0.99, linear_interp=0)


def resample(
        input_path,
        output_path,
        rate,
        channels,
        start=None,
        duration=None,
        loop=False,
        hq=False):
    """Convert to `rate`/`channels` s16. `hq` opts into the HQ_SWR filter; it is
    used for the music corpora, while speech keeps ffmpeg's defaults."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    try:
        input_args = {}
        output_args = {}

        if loop:
            # Loop input indefinitely, then trim to requested duration
            input_args['stream_loop'] = -1

        if start is not None:
            output_args['ss'] = start
        if duration is not None:
            output_args['t'] = duration

        stream = ffmpeg.input(input_path, **input_args)
        if hq:
            stream = stream.filter('aresample', rate, **HQ_SWR)
            rate_args = {}
        else:
            rate_args = {'ar': rate}
        (stream.output(output_path,
                       ac=channels,
                       sample_fmt='s16',
                       **rate_args,
                       **output_args).run(quiet=True, overwrite_output=True))
    except ffmpeg.Error as e:
        print(
            f" FFmpeg error during setup: {
                e.stderr.decode() if e.stderr else e}")


def get_tier_params(dur):
    """
    Determine resampling parameters based on ViSQOL recommendations (5-10s).
    1. < 5s: loop to 5s
    2. 5-10s: use full sample
    3. > 10s: trim to 10s center segment
    """
    if dur < 5.0:
        return 0, 5, True
    if dur <= 10.0:
        return None, None, False
    return (dur - 10) / 2, 10, False


def pmlt_wav_files(src_dir):
    wav_files = []
    for root, dirs, files in os.walk(src_dir):
        for f in files:
            if f.endswith("48k.wav") and not re.search(r"48k\.\d+\.wav$", f):
                wav_files.append(os.path.join(root, f))
    return wav_files


def soundexpert_wav_files(src_dir):
    wav_files = []
    for root, dirs, files in os.walk(src_dir):
        for f in files:
            if f.endswith(".wav"):
                wav_files.append(os.path.join(root, f))
    return wav_files


def _tcd_wav_files(src_dir, clean):
    """Walk the TCD-VoIP tree for either the clean references or the degraded set.

    The `ref/` folders hold the undegraded source recordings. They used to be
    skipped outright, which meant every speech scenario measured codec
    artifacts on top of pre-existing chop/clip/echo/noise -- the degradation
    sits in ViSQOL's reference, widening the variance a real regression has to
    clear. They are now their own corpus.
    """
    wav_files = []
    for root, dirs, files in os.walk(src_dir):
        is_ref = "ref" in root.split(os.sep)
        if is_ref != clean:
            continue
        for f in files:
            if not f.endswith(".wav"):
                continue
            if clean or "Test Set" in root or "chop" in root:
                wav_files.append(os.path.join(root, f))
    return wav_files


def _build_speech_corpus(src_dir, dest_dir, rate, label):
    """Resample a set of TCD-VoIP WAVs into one fixed-format corpus."""
    clean = "clean" in os.path.basename(dest_dir)
    wav_files = _tcd_wav_files(src_dir, clean=clean)

    print(f"Found {len(wav_files)} valid samples for {label}.")
    skipped = 0
    for i, wav in enumerate(wav_files):
        dur, chans = get_info(wav)
        src_rate = get_rate(wav)
        # Never upsample into a corpus: a 24 kHz corpus built from 16 kHz
        # sources would be inventing bandwidth the benchmark then claims to
        # measure.
        if src_rate and src_rate < rate:
            skipped += 1
            continue
        print(f"  [{i + 1}/{len(wav_files)}] Processing {os.path.basename(wav)}...")
        start, duration, loop = get_tier_params(dur)
        output = os.path.join(dest_dir, os.path.basename(wav))
        resample(wav, output, rate, 1, start=start, duration=duration, loop=loop)

    if skipped:
        print(f"  WARNING: skipped {skipped} source(s) below {rate} Hz rather than "
              f"upsampling them into {os.path.basename(dest_dir)}.")


def setup_tcd_voip():
    """The degraded VoIP test set (data/external/speech)."""
    dataset_info = DATASETS["TCD-VOIP"]
    src_dir = os.path.join(TEMP_DIR, "TCD-VOIP-harte2015tcd")
    # ViSQOL speech mode requires 16k mono
    _build_speech_corpus(src_dir, os.path.join(BASE_DATA_DIR, "speech"),
                         16000, dataset_info["name"])


def setup_tcd_ref(rate, dest_name):
    """The clean reference recordings, at `rate` mono."""
    src_dir = os.path.join(TEMP_DIR, "TCD-VOIP-harte2015tcd")
    _build_speech_corpus(src_dir, os.path.join(BASE_DATA_DIR, dest_name),
                         rate, f"TCD-VoIP clean references @ {rate} Hz")


def setup_audio_51():
    """Download or generate the 5.1 surround 6_Channel_ID.wav reference sample."""
    dest_dir = os.path.join(BASE_DATA_DIR, "audio_51")
    os.makedirs(dest_dir, exist_ok=True)
    out_path = os.path.join(dest_dir, "6_Channel_ID.wav")
    if not os.path.exists(out_path):
        dataset_info = DATASETS.get("FFmpeg51", {})
        url = dataset_info.get("url")
        download_ok = False
        if url:
            print(f"Downloading {dataset_info.get('name', '5.1 Surround Reference')}...")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                tmp_path = out_path + ".tmp"
                with urllib.request.urlopen(req) as response, open(tmp_path, 'wb') as out:
                    shutil.copyfileobj(response, out)
                shutil.move(tmp_path, out_path)
                download_ok = True
            except Exception as e:
                print(f"Download failed ({e}), falling back to generating 5.1 surround sample with FFmpeg...")

        if not download_ok:
            print(f"Generating 5.1 surround sample {out_path} with FFmpeg...")
            filter_expr = "aevalsrc='sin(2*PI*440*t)|sin(2*PI*880*t)|sin(2*PI*220*t)|sin(2*PI*110*t)|sin(2*PI*550*t)|sin(2*PI*660*t)':d=10"
            try:
                (
                    ffmpeg
                    .input(filter_expr, format='lavfi')
                    .output(out_path, ar=44100, sample_fmt='s16')
                    .run(quiet=True, overwrite_output=True)
                )
            except ffmpeg.Error as e:
                print(f"FFmpeg error generating 5.1 surround sample: {e.stderr.decode() if e.stderr else e}")


# Clips in the newer listening-test sets that are the same recording as one
# already supplied by PMLT2014/SoundExpert, found offline by content (the names
# and cuts differ) with scripts/find_duplicate_clips.py and committed in
# duplicate_clips.json so setup never has to re-derive them. Each entry is
# "replace" (the lossless FLAC supersedes the older clip: it is built under its
# own name and the older clip is left out of every corpus) or "skip" (the FLAC
# is left out and the older clip kept). "matches" names the older clip.
DUPLICATES_FILE = os.path.join(SCRIPT_DIR, "duplicate_clips.json")


def load_duplicates():
    with open(DUPLICATES_FILE) as f:
        return json.load(f)["duplicates"]


def source_info(path):
    """(duration, channels, sample rate) of a music source, WAV or FLAC."""
    if path.endswith(".wav"):
        dur, chans = get_info(path)
        return dur, chans, get_rate(path)
    dur, chans = get_info_ffprobe(path)
    try:
        stream = next(st for st in ffmpeg.probe(path)["streams"] if st["codec_type"] == "audio")
        return dur, chans, int(stream["sample_rate"])
    except BaseException:
        return dur, chans, 0


MUSIC_DATASETS = ["PMLT2014", "SoundExpert", "IgorCPBLT2020", "Kamedo2PLT2012"]


def music_sources():
    """Every clip of the music corpora as (out_name, source path), in build order.

    One list drives `audio` and the corpora derived from it, so each of them
    can resample from the original instead of from another corpus.

    Only the top-level reference FLACs of the listening-test sets are used:
    IgorCPBLT2020 also ships a lossy encode of each clip per codec in
    subfolders, which would be measuring other encoders' artifacts as reference
    material. A clip that duplicates an earlier one (duplicate_clips.json) either
    supersedes it (the older clip is dropped from the list) or is skipped, so
    each recording is scored once.
    """
    dupes = load_duplicates()
    sources = {}
    for wav in pmlt_wav_files(os.path.join(TEMP_DIR, "PMLT2014-PMLT2014")):
        sources[os.path.basename(wav)] = wav
    for wav in soundexpert_wav_files(os.path.join(TEMP_DIR, "SoundExpert-SoundExpert")):
        sources[os.path.basename(wav)] = wav
    for zip_name in ("IgorCPBLT2020", "Kamedo2PLT2012"):
        src_dir = os.path.join(TEMP_DIR, f"{zip_name}-{zip_name}")
        for filename in sorted(f for f in os.listdir(src_dir) if f.endswith(".flac")):
            dupe = dupes.get(f"{zip_name}/{filename}")
            if dupe and dupe["action"] == "skip":
                print(f"  Skipping {filename} (duplicate of {dupe['matches']}).")
                continue
            if dupe:
                print(f"  {filename} supersedes {dupe['matches']}.")
                sources.pop(dupe["matches"], None)
            sources[os.path.splitext(filename)[0] + ".wav"] = os.path.join(src_dir, filename)
    return list(sources.items())


def superseded_clips():
    """Older clips that a lossless copy in a newer set replaces."""
    return {d["matches"] for d in load_duplicates().values() if d["action"] == "replace"}


def _remove_superseded(dest_dir):
    """Drop superseded clips a previous build left behind, so a --force rebuild
    of an existing checkout does not score the same recording twice."""
    for name in sorted(superseded_clips()):
        path = os.path.join(dest_dir, name)
        if os.path.exists(path):
            print(f"  Removing superseded {name} from {os.path.basename(dest_dir)}.")
            os.remove(path)


def setup_audio():
    """The 48 kHz music corpus."""
    sources = music_sources()
    _remove_superseded(os.path.join(BASE_DATA_DIR, "audio"))
    print(f"Found {len(sources)} music samples for "
          f"{', '.join(DATASETS[n]['name'] for n in MUSIC_DATASETS)}.")
    for i, (out_name, src) in enumerate(sources):
        print(f"  [{i + 1}/{len(sources)}] Processing {out_name}...")
        dur, chans, _ = source_info(src)
        start, duration, loop = get_tier_params(dur)
        resample(src, os.path.join(BASE_DATA_DIR, "audio", out_name), 48000, chans,
                 start=start, duration=duration, loop=loop, hq=True)


def derives_from_original(src_rate, rate):
    """A derived corpus resamples the original when it has at least the target
    rate, so a native 44.1 kHz clip is not pushed through 48 kHz and back. A
    source below the target would have to be upsampled; it falls back to the 48
    kHz clip, as before."""
    return src_rate >= rate


def setup_derived_audio(dest_name, rate):
    """Build a music corpus at `rate`, resampling each original once.

    Downsampling is honest here -- the derived file is itself the reference
    every metric scores against, so the comparison stays self-consistent.
    """
    sources = music_sources()
    audio_dir = os.path.join(BASE_DATA_DIR, "audio")
    dest_dir = os.path.join(BASE_DATA_DIR, dest_name)
    _remove_superseded(dest_dir)
    print(f"Found {len(sources)} samples to derive {dest_name} @ {rate} Hz.")
    for i, (out_name, src) in enumerate(sources):
        dur, _, src_rate = source_info(src)
        out = os.path.join(dest_dir, out_name)
        if derives_from_original(src_rate, rate):
            print(f"  [{i + 1}/{len(sources)}] Processing {out_name} from the original "
                  f"({src_rate} Hz)...")
            start, duration, loop = get_tier_params(dur)
            resample(src, out, rate, 2, start=start, duration=duration, loop=loop, hq=True)
        else:
            print(f"  [{i + 1}/{len(sources)}] Processing {out_name} from the 48 kHz clip "
                  f"(source is {src_rate} Hz)...")
            resample(os.path.join(audio_dir, out_name), out, rate, 2, hq=True)


def setup_throughput_signals():
    """Generate 10-minute test signals for throughput measurement."""
    dest_dir = os.path.join(BASE_DATA_DIR, "throughput")
    os.makedirs(dest_dir, exist_ok=True)

    signals = {
        "sine": "sine=f=440:d=600",
        "sweep": "aevalsrc='sin(2*PI*(100+(20000-100)/(2*600)*t)*t)':d=600",
        "noise": "anoisesrc=d=600",
        "silence": "anullsrc=d=600"
    }

    print(f"Generating 10-minute throughput signals...")
    for name, filter_str in signals.items():
        output_path = os.path.join(dest_dir, f"{name}.wav")
        if not os.path.exists(output_path):
            print(f"  Generating {name}.wav...")
            try:
                # Note: aevalsrc is also a lavfi filter
                (
                    ffmpeg
                    .input(filter_str, format='lavfi')
                    .output(output_path, ar=48000, ac=2, sample_fmt='s16')
                    .run(quiet=True, overwrite_output=True)
                )
            except ffmpeg.Error as e:
                print(
                    f" FFmpeg error during signal generation: {
                        e.stderr.decode() if e.stderr else e}")

    setup_realistic_throughput_signals(dest_dir)


def setup_realistic_throughput_signals(dest_dir):
    """Throughput signals built from real music, looped to ~10 minutes.

    Sine, sweep, noise and silence are stable to time but they barely exercise
    the parts of the encoder whose cost actually changes: block switching sees
    no transients, TNS never fires, and stereo coding sees no realistic
    correlation. A change can move real-content encode time by 20% and leave
    all four synthetic signals flat.

    Each source is one of the music gate clips, so throughput and quality are
    measured on the same content.
    """
    from config import _MUSIC_GATE

    src_dir = os.path.join(BASE_DATA_DIR, "audio")
    if not os.path.isdir(src_dir):
        print(f"  WARNING: {src_dir} missing; throughput will measure synthetic "
              f"signals only, which cannot see block-switching changes.")
        return

    # Two clips, chosen for opposite encoder behaviour: percussive content that
    # drives short blocks, and tonal content that stays long.
    for tag, clip in (("music_percussive", _MUSIC_GATE[0]),
                      ("music_tonal", _MUSIC_GATE[1])):
        src = os.path.join(src_dir, clip)
        out = os.path.join(dest_dir, f"{tag}.wav")
        if os.path.exists(out):
            continue
        if not os.path.exists(src):
            # Silence here is expensive: the signal is skipped, the dataset is
            # cached without it, and every later run inherits the gap while the
            # report still shows a throughput number.
            print(f"  WARNING: {clip} not found in {src_dir}; skipping {tag}. "
                  f"Throughput will not see real content.")
            continue
        print(f"  Generating {tag}.wav from {clip}...")
        try:
            (
                ffmpeg
                .input(src, stream_loop=-1, t=600)
                .output(out, ar=48000, ac=2, sample_fmt='s16')
                .run(quiet=True, overwrite_output=True)
            )
        except ffmpeg.Error as e:
            print(f" FFmpeg error building {tag}: "
                  f"{e.stderr.decode() if e.stderr else e}")


# Which builder produces each corpus directory, and which dataset zips it needs.
# Keyed by the "dir" of each entry in config.CORPORA.
CORPUS_BUILDERS = {
    "audio": (MUSIC_DATASETS, setup_audio),
    "speech": (["TCD-VOIP"], setup_tcd_voip),
    "speech_clean_16k": (["TCD-VOIP"], lambda: setup_tcd_ref(16000, "speech_clean_16k")),
    "speech_clean_24k": (["TCD-VOIP"], lambda: setup_tcd_ref(24000, "speech_clean_24k")),
    # Resampled once from the same originals as "audio" rather than from it, so
    # a native 44.1 kHz clip is not converted 44.1 -> 48 -> 44.1. A source below
    # the target still falls back to the "audio" clip, which build order below
    # guarantees exists.
    "audio_44k1": (MUSIC_DATASETS, lambda: setup_derived_audio("audio_44k1", 44100)),
    "audio_32k": (MUSIC_DATASETS, lambda: setup_derived_audio("audio_32k", 32000)),
    "audio_51": ([], setup_audio_51),
}

# Build order matters: the derived music corpora read data/external/audio.
BUILD_ORDER = ["audio", "speech", "speech_clean_16k", "speech_clean_24k",
               "audio_44k1", "audio_32k", "audio_51"]


def corpus_is_populated(dirname):
    path = os.path.join(BASE_DATA_DIR, dirname)
    return os.path.isdir(path) and any(f.endswith(".wav") for f in os.listdir(path))


def main():
    parser = argparse.ArgumentParser(
        description="Download and build the reference corpora.")
    parser.add_argument("--force", action="store_true",
                        help="Rebuild every corpus even if it already exists.")
    args = parser.parse_args()

    # Per-corpus, not all-or-nothing. The old guard was
    # `if not os.path.exists(BASE_DATA_DIR)`, so an existing checkout could
    # never gain a corpus that was added later -- it would silently run with
    # whatever it happened to have.
    todo = [d for d in BUILD_ORDER
            if args.force or not corpus_is_populated(d)]

    if todo:
        needed_zips = []
        for dirname in todo:
            for zip_name in CORPUS_BUILDERS[dirname][0]:
                if zip_name not in needed_zips:
                    needed_zips.append(zip_name)

        for zip_name in needed_zips:
            download_and_extract(zip_name, DATASETS[zip_name]["url"])

        for dirname in todo:
            print(f"\n>>> Building corpus: {dirname}")
            CORPUS_BUILDERS[dirname][1]()

        if "audio" in todo:
            # The realistic throughput signals are cut from corpus clips and
            # setup skips ones that already exist, so a rebuilt `audio` would
            # leave them on the old content ("same content as quality").
            for stale in glob.glob(os.path.join(BASE_DATA_DIR, "throughput", "music_*.wav")):
                os.remove(stale)

        if os.path.exists(TEMP_DIR):
            shutil.rmtree(TEMP_DIR)
    else:
        print("All corpora already present.")

    # Always check for throughput signals as they are vital for stable metrics
    setup_throughput_signals()
    print("Done.")


if __name__ == "__main__":
    main()
