#!/bin/bash
# arms.sh MODEARGS TAG "name:ENV=.. ENV=.." ...
S=$(pwd); A=/Users/nschimme/gitprojects/faac-benchmark/scripts/align; IN=${IN:-$A/bursts.wav}; F=${F:-/Users/nschimme/gitprojects/faac/.cache/probe/b/frontend/faac}
M=$1; T=$2; shift 2
for v in "$@"; do n=${v%%:*}; rm -f t.$T.$n.m4a; env ${v#*:} $F $M -o t.$T.$n.m4a $IN >/dev/null 2>&1; ffmpeg -v error -y -i t.$T.$n.m4a x.$T.$n.wav; echo "$n size=$(stat -f%z t.$T.$n.m4a) $(cd $A; ../../.venv/bin/python lfhf.py $S/x.$T.$n.wav 2>/dev/null | cut -d' ' -f2-)"; done
