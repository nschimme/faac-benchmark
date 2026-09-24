#!/bin/bash
# ident.sh NEWBIN REFBIN "objtype rate" ... : decoded-md5 identity check on two clips
N=$1; R=$2; shift 2
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
A="$SCRIPT_DIR/../../data/external/audio"
T=$(mktemp -d)
for spec in "$@"; do
  set -- $spec
  for c in sandman.16b48k fms; do
    in=$(ls $A/$c*.wav | head -1)
    rm -f $T/n.m4a $T/r.m4a
    $N -b $2 --object-type $1 -o $T/n.m4a "$in" >/dev/null 2>&1
    $R -b $2 --object-type $1 -o $T/r.m4a "$in" >/dev/null 2>&1
    a=$(ffmpeg -v error -i $T/n.m4a -f md5 -); b=$(ffmpeg -v error -i $T/r.m4a -f md5 -)
    [ "$a" = "$b" ] && echo "$1 $2 $c identical" || echo "$1 $2 $c DIFFERS"
  done
done
rm -rf $T
