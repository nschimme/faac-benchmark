#!/bin/sh
#
# Entrypoint wrapper for qaac64 container.
# Measures execution time inside the container to exclude Docker/Podman startup overhead.
#

start_time=$(date +%s.%N 2>/dev/null)
if [ -z "$start_time" ] || [ "$start_time" = "%s.%N" ]; then
    start_time=$(date +%s)
fi

wine64 /qaac/qaac64.exe "$@"
ret=$?

end_time=$(date +%s.%N 2>/dev/null)
if [ -z "$end_time" ] || [ "$end_time" = "%s.%N" ]; then
    end_time=$(date +%s)
fi

duration=$(awk -v s="$start_time" -v e="$end_time" 'BEGIN { printf "%.6f", e - s }' 2>/dev/null)
if [ -n "$duration" ]; then
    echo "__QAAC_TIME__:$duration" >&2
fi

exit $ret
