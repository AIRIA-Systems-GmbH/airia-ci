#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# Samples the container once a second so the next burst can be read (#27, POLICY.md "Container
# resources"). Read-only: it only reads /proc; it writes nothing but its output. Run it inside the
# container as any user, before the burst starts:
#
#   measure.sh [seconds [outfile]]      default 60 seconds, standard output; an outfile is appended to
#
# One line per sample:
#   <UTC time> load=<1m>,<5m>,<15m> psi_cpu=<n> psi_io=<n> psi_mem=<n> mem_avail_mb=<n> listen=<n> new=<ports>
# psi_* is "some avg10" of /proc/pressure/{cpu,io,memory}; mem_avail_mb is MemAvailable; listen is
# the number of distinct listening TCP ports (IPv4 and IPv6); new lists the listening ports that
# were not there in the previous sample ("-" for none, and always on the first sample, which is the
# baseline). A value the kernel does not offer (no /proc on macOS, no pressure files in a container
# without PSI) is "NA".
#
# MEASURE_PROC is the /proc root (default /proc); MEASURE_INTERVAL the seconds between samples
# (default 1). Both exist so the tests can feed a fake /proc.
set -euo pipefail

seconds="${1:-60}"
out="${2:-}"
proc="${MEASURE_PROC:-/proc}"
interval="${MEASURE_INTERVAL:-1}"

case "$seconds" in
  '' | *[!0-9]*) echo "usage: measure.sh [seconds [outfile]]" >&2; exit 2 ;;
esac
[ -z "$out" ] || exec >> "$out"

psi() { # "some avg10=0.28 ..." -> 0.28
  local v; v="$(awk '$1=="some"{sub("avg10=","",$2); print $2}' "$proc/pressure/$1" 2> /dev/null || true)"
  echo "${v:-NA}"
}

load() {
  local v; v="$(awk 'NR==1{print $1 "," $2 "," $3}' "$proc/loadavg" 2> /dev/null || true)"
  echo "${v:-NA}"
}

mem_avail_mb() {
  local kb; kb="$(awk '$1=="MemAvailable:"{print $2}' "$proc/meminfo" 2> /dev/null || true)"
  if [ -n "$kb" ]; then echo $((kb / 1024)); else echo NA; fi
}

# Distinct listening TCP ports, one decimal per line (state 0A in /proc/net/tcp and tcp6).
listening() {
  local f hex
  for f in "$proc/net/tcp" "$proc/net/tcp6"; do
    [ -r "$f" ] || continue
    for hex in $(awk 'FNR>1 && $4=="0A"{n=split($2, a, ":"); print a[n]}' "$f"); do
      echo $((16#$hex))
    done
  done | sort -un
}

previous=""
seen=""
sample() {
  local ports new="" p
  ports="$(listening)"
  for p in $ports; do
    case " $previous " in *" $p "*) ;; *) new="${new:+$new,}$p" ;; esac
  done
  # The first sample has nothing to compare with.
  [ -n "$seen" ] || new=""
  seen=1
  previous="$(echo "$ports" | tr '\n' ' ')"
  printf '%s load=%s psi_cpu=%s psi_io=%s psi_mem=%s mem_avail_mb=%s listen=%s new=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(load)" "$(psi cpu)" "$(psi io)" "$(psi memory)" \
    "$(mem_avail_mb)" "$(echo "$ports" | grep -c . || true)" "${new:--}"
}

i=0
while [ "$i" -lt "$seconds" ]; do
  sample
  i=$((i + 1))
  [ "$i" -ge "$seconds" ] || sleep "$interval"
done
