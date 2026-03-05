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

SCENARIOS = {
    "voip": {
        "mode": "speech",
        "rate": 16000,
        "visqol_rate": 16000,
        "bitrate": 16,
        "thresh": 2.5},
    "vss": {
        "mode": "speech",
        "rate": 16000,
        "visqol_rate": 16000,
        "bitrate": 40,
        "thresh": 3.0},
    "music_low": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 64,
        "thresh": 3.5},
    "music_std": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 128,
        "thresh": 4.0},
    "music_high": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 256,
        "thresh": 4.3}}
