#!/bin/bash
# usage: ab.sh FAACBIN "clips" "modes(;-sep)" knobvar "vals"
FB=/Users/nschimme/gitprojects/faac-benchmark; F=$1
IFS=';' read -ra MODES <<< "$3"
for c in $2; do IN=$(ls $FB/data/external/audio/$c*.wav|head -1)
 for m in "${MODES[@]}"; do for k in $5; do
  rm -f p_$$.m4a; env $4=$k $F $m -o p_$$.m4a $IN >/dev/null 2>&1; sz=$(stat -f%z p_$$.m4a)
  $FB/bin/fdkdec p_$$.m4a p_$$_fdk.wav >/dev/null 2>&1; ffmpeg -v error -y -i p_$$.m4a p_$$_ff.wav
  echo "$c [$m] $4=$k size=$sz fdk=$($FB/.venv/bin/python sc.py $IN p_$$_fdk.wav 2>/dev/null|cut -c1-5) ff=$($FB/.venv/bin/python sc.py $IN p_$$_ff.wav 2>/dev/null|cut -c1-5)"
 done; done; done
