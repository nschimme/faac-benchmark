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


def resample(
        input_path,
        output_path,
        rate,
        channels,
        start=None,
        duration=None,
        loop=False):
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

        (ffmpeg .input(input_path,
                       **input_args) .output(output_path,
                                             ar=rate,
                                             ac=channels,
                                             sample_fmt='s16',
                                             **output_args) .run(quiet=True,
                                                                 overwrite_output=True))
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


def setup_pmlt():
    dataset_info = DATASETS["PMLT2014"]
    src_dir = os.path.join(TEMP_DIR, "PMLT2014-PMLT2014")
    dest_dir = os.path.join(BASE_DATA_DIR, "audio")

    wav_files = []
    for root, dirs, files in os.walk(src_dir):
        for f in files:
            if f.endswith("48k.wav") and not re.search(r"48k\.\d+\.wav$", f):
                wav_files.append(os.path.join(root, f))

    print(f"Found {len(wav_files)} valid samples for {dataset_info['name']}.")
    for i, wav in enumerate(wav_files):
        print(f"  [{i + 1}/{len(wav_files)}] Processing {os.path.basename(wav)}...")
        dur, chans = get_info(wav)
        start, duration, loop = get_tier_params(dur)

        filename = os.path.basename(wav)
        output = os.path.join(dest_dir, filename)
        resample(
            wav,
            output,
            48000,
            chans,
            start=start,
            duration=duration,
            loop=loop)


def setup_tcd_voip():
    dataset_info = DATASETS["TCD-VOIP"]
    src_dir = os.path.join(TEMP_DIR, "TCD-VOIP-harte2015tcd")
    dest_dir = os.path.join(BASE_DATA_DIR, "speech")

    wav_files = []
    for root, dirs, files in os.walk(src_dir):
        # Do not use any wave files if they're in a "ref" folder
        if "ref" in root.split(os.sep):
            continue

        for f in files:
            if f.endswith(".wav") and ("Test Set" in root or "chop" in root):
                wav_files.append(os.path.join(root, f))

    print(f"Found {len(wav_files)} valid samples for {dataset_info['name']}.")
    for i, wav in enumerate(wav_files):
        print(f"  [{i + 1}/{len(wav_files)}] Processing {os.path.basename(wav)}...")
        dur, chans = get_info(wav)
        start, duration, loop = get_tier_params(dur)

        filename = os.path.basename(wav)
        output = os.path.join(dest_dir, filename)
        # ViSQOL speech mode requires 16k mono
        resample(
            wav,
            output,
            16000,
            1,
            start=start,
            duration=duration,
            loop=loop)


def setup_soundexpert():
    dataset_info = DATASETS["SoundExpert"]
    src_dir = os.path.join(TEMP_DIR, "SoundExpert-SoundExpert")
    dest_dir = os.path.join(BASE_DATA_DIR, "audio")

    wav_files = []
    for root, dirs, files in os.walk(src_dir):
        for f in files:
            if f.endswith(".wav"):
                wav_files.append(os.path.join(root, f))

    print(f"Found {len(wav_files)} valid samples for {dataset_info['name']}.")
    for i, wav in enumerate(wav_files):
        print(f"  [{i + 1}/{len(wav_files)}] Processing {os.path.basename(wav)}...")
        dur, chans = get_info(wav)
        start, duration, loop = get_tier_params(dur)

        filename = os.path.basename(wav)
        output = os.path.join(dest_dir, filename)
        resample(
            wav,
            output,
            48000,
            chans,
            start=start,
            duration=duration,
            loop=loop)


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
        return

    # Two clips, chosen for opposite encoder behaviour: percussive content that
    # drives short blocks, and tonal content that stays long.
    for tag, clip in (("music_percussive", _MUSIC_GATE[0]),
                      ("music_tonal", _MUSIC_GATE[1])):
        src = os.path.join(src_dir, clip)
        out = os.path.join(dest_dir, f"{tag}.wav")
        if not os.path.exists(src) or os.path.exists(out):
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


if __name__ == "__main__":
    if not os.path.exists(BASE_DATA_DIR):
        for name, info in DATASETS.items():
            download_and_extract(name, info["url"])

        setup_pmlt()
        setup_tcd_voip()
        setup_soundexpert()
        setup_throughput_signals()

        if os.path.exists(TEMP_DIR):
            shutil.rmtree(TEMP_DIR)
    else:
        # Always check for throughput signals as they are vital for stable
        # metrics
        setup_throughput_signals()
        print("Datasets already setup.")
    print("Done.")
