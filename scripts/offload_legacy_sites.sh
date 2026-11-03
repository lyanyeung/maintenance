#!/usr/bin/env bash
# One-shot offload of the remaining legacy SPINUP cases at the given sites:
#   plan (repeated: same-site, non-FIXNC cases that depend on them are added with --extra-case)
#   -> archive (tar + read-back SHA256 on the Research Filestore) -> clean (delete from Scratch).
# Stops at the first problem: a case held for any other reason (live job, Git lock, recent
# writes, path problem), a dependant at another site or a FIXNC/OBSPFT dependant, an archive
# skip. Nothing is deleted unless every planned case was archived and verified.
#
# Usage:  bash offload_legacy_sites.sh DK-Sor IT-Noe
# Needs offload_legacy_spinup1000.py (+ its two helper modules) in $TOOLDIR.
set -euo pipefail
TOOLDIR=${TOOLDIR:-$HOME/maintenance_tools/legacy1000}
TOOL=$TOOLDIR/offload_legacy_spinup1000.py
PY=${PY:-python3}
TOOL_ARGS=${TOOL_ARGS:-}          # extra options for every mode (tests only)
TOKEN=DELETE_VERIFIED_LEGACY_SPINUP1000
[ $# -ge 1 ] || { echo "usage: $0 SITE [SITE...]"; exit 2; }
[ -f "$TOOL" ] || { echo "STOP: $TOOL not found"; exit 1; }
SITES=("$@")
STAMP=$(date +%Y%m%d_%H%M%S)
LOGDIR=$HOME/legacy1000_logs
mkdir -p "$LOGDIR"
echo "== offload_legacy_sites  sites: ${SITES[*]}  logs: $LOGDIR/*_$STAMP*"

extra=()
plan_file=""
for round in 1 2 3 4 5 6; do
  args=(plan --sites "${SITES[@]}")
  [ ${#extra[@]} -gt 0 ] && args+=(--extra-case "${extra[@]}")
  out=$LOGDIR/plan_${STAMP}_r$round.txt
  # shellcheck disable=SC2086
  if ! "$PY" -B "$TOOL" "${args[@]}" $TOOL_ARGS > "$out" 2>&1; then
    cat "$out"; echo "STOP: plan failed"; exit 1
  fi
  echo "-- plan round $round${extra:+ (extra: ${extra[*]})}"
  grep -E $'^(OFFLOAD|HELD)\t' "$out" | grep -vE $'^(OFFLOAD\tbatch|HELD\tcase)\t' || true
  new=()
  bad=0
  while IFS=$'\t' read -r _ case state reason; do
    dep=$(printf '%s\n' "$reason" | sed -n \
      -e 's/.*referenced by remaining case \([^ ;]*\).*/\1/p' \
      -e 's/.*RUNDIR symlink of \([^ ;]*\) points into.*/\1/p' | head -1)
    if [ -z "$dep" ]; then
      echo "STOP: $case is held ($state): $reason"; bad=1; continue
    fi
    same_site=0
    for s in "${SITES[@]}"; do [[ $dep == "${s}_"* ]] && same_site=1; done
    if [ $same_site -eq 1 ] && [[ $dep != *FIXNC* && $dep != *OBSPFT* ]]; then
      new+=("$dep")
    else
      echo "STOP: $case is needed by $dep (other site or FIXNC/OBSPFT case; not added automatically)"; bad=1
    fi
  done < <(grep -E $'^HELD\t' "$out" | grep -vE $'^HELD\tcase\t' || true)
  [ $bad -eq 0 ] || { echo "Plan: $out"; exit 1; }
  if [ ${#new[@]} -eq 0 ]; then
    plan_file=$(sed -n 's/^PLAN_WRITTEN //p' "$out")
    break
  fi
  for d in "${new[@]}"; do
    [[ " ${extra[*]:-} " == *" $d "* ]] || extra+=("$d")
  done
done
[ -n "$plan_file" ] || { echo "STOP: dependants still unresolved after 6 rounds"; exit 1; }
n=$(grep -cE $'^OFFLOAD\t' "$out" || true); n=$((n - 1))
grep -E '^(NOT_SELECTED|ORPHAN_OUTPUT|  |TOTAL)' "$out" || true
[ $n -gt 0 ] || { echo "Nothing to offload for: ${SITES[*]} (cases already gone?)"; exit 0; }
echo "== $n case(s) to archive and delete; plan $plan_file"

alog=$LOGDIR/archive_$STAMP.log
# shellcheck disable=SC2086
"$PY" -B "$TOOL" archive --plan "$plan_file" $TOOL_ARGS 2>&1 | tee "$alog" || true
done_line=$(grep '^ARCHIVE_DONE' "$alog" || true)
session=$(sed -n 's/^SESSION //p' "$alog")
if [ -z "$session" ] || [[ $done_line != *"verified=$n skipped=0"* ]]; then
  echo "STOP: archive did not verify all $n case(s); nothing deleted. Log: $alog"; exit 1
fi

clog=$LOGDIR/clean_$STAMP.log
# shellcheck disable=SC2086
"$PY" -B "$TOOL" clean --session "$session" --confirm "$TOKEN" $TOOL_ARGS 2>&1 | tee "$clog"
grep -q '^CLEAN_DONE' "$clog" || { echo "STOP: clean did not finish. Log: $clog"; exit 1; }
command -v myfiles >/dev/null 2>&1 && myfiles || true
echo "== ALL DONE  session=$session"
