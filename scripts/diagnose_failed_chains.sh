#!/bin/bash
# READ ONLY. Explain every broken CIME run chain in one go and say what to do next.
#
# A broken chain shows up as a st_archive job "PD (DependencyNeverSatisfied)": its case.run
# did not end OK. For each such job (or for the CASE names / job ids given as arguments) this
# prints: case, run job state/exit/elapsed/node, last CaseStatus events, RESUBMIT/CONTINUE_RUN,
# latest archived restart vs rpointer, the batch job log tail, the error lines and tail of the
# cesm.log / lnd.log written by that run, then a VERDICT and the next step.
#
# Nothing is cancelled, submitted or changed.
#   bash diagnose_failed_chains.sh                  # all DependencyNeverSatisfied jobs
#   bash diagnose_failed_chains.sh 4820160 CASE ... # specific st_archive/run job ids or case names
# Run it as a separate process with output in a file, so a dropped SSH session loses nothing:
#   setsid bash ~/maintenance/scripts/diagnose_failed_chains.sh > ~/diag_chains.txt 2>&1 < /dev/null; cat ~/diag_chains.txt
#
# Every command has a timeout and no command's failure stops the script (no set -e).

VERSION=20261104-v1
CR=${CASES_ROOT:-/iridisfs/scratch/ly3n24/CLM_FATES/cases}
U=${USER:-$(whoami)}
T=timeout
ERR_RE='ENDRUN|abort|ERROR|error:|NaN|nan |SIGSEGV|SIGBUS|segmentation|floating|balance|negative|MPI_ABORT|forrtl|Killed|out of memory|oom|DUE TO TIME LIMIT|CANCELLED'

hr() { printf '%s\n' "------------------------------------------------------------------------"; }
xq() { (cd "$1" && $T 120 ./xmlquery --value "$2" 2>/dev/null); }

# ---- resolve the work list: lines "case|run_jobid|st_archive_jobid"
declare -a WORK
add_from_starchive() {   # $1 = st_archive job id (must still be in squeue/scontrol)
  local info dep wd
  info=$($T 30 scontrol show job "$1" 2>/dev/null) || return 1
  dep=$(grep -oE 'after(ok|any|notok)?:[0-9]+' <<<"$info" | head -1 | grep -oE '[0-9]+$')
  wd=$(grep -oE 'WorkDir=[^ ]+' <<<"$info" | cut -d= -f2)
  [ -n "$wd" ] || return 1
  WORK+=("$(basename "$wd")|${dep:-?}|$1")
}
add_from_case() {        # $1 = case name: last case.run job id from CaseStatus
  local c=$1 rid sa
  [ -f "$CR/$c/CaseStatus" ] || { WORK+=("$c|?|?"); return; }
  rid=$(grep -oE 'case.run (starting|success|error) [0-9]+' "$CR/$c/CaseStatus" | tail -1 | grep -oE '[0-9]+$')
  [ -z "$rid" ] && rid=$(grep -oE 'case.run:[0-9]+' "$CR/$c/CaseStatus" | tail -1 | cut -d: -f2)
  sa=$($T 30 squeue -u "$U" -h -o '%i %j' 2>/dev/null | awk -v c="st_archive.$c" '$2==c{print $1}' | head -1)
  WORK+=("$c|${rid:-?}|${sa:--}")
}

if [ $# -eq 0 ]; then
  for j in $($T 30 squeue -u "$U" -h -t PD -o '%i %r' 2>/dev/null | awk '$2=="DependencyNeverSatisfied"{print $1}'); do
    add_from_starchive "$j"
  done
else
  for a in "$@"; do
    if [[ $a =~ ^[0-9]+$ ]]; then
      if ! add_from_starchive "$a"; then          # not a queued st_archive: treat as a run job id
        wd=$($T 30 sacct -j "$a" -X -n -P -o WorkDir 2>/dev/null | head -1)
        WORK+=("$(basename "${wd:-unknown}")|$a|-")
      fi
    else
      add_from_case "$a"
    fi
  done
fi

echo "diagnose_failed_chains $VERSION  host=$(hostname)  $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "cases root: $CR"
if [ ${#WORK[@]} -eq 0 ]; then echo "No DependencyNeverSatisfied jobs and no arguments: nothing broken."; exit 0; fi

declare -a SUMMARY
for w in "${WORK[@]}"; do
  IFS='|' read -r c rid sa <<<"$w"
  C="$CR/$c"
  echo; echo "########################################################################"
  echo "CASE $c   run=$rid   orphan st_archive=$sa"
  hr
  verdict=UNKNOWN; next="look at the log excerpts below"
  if [ ! -d "$C" ]; then
    echo "case folder not found"; SUMMARY+=("$c|NO_CASE_DIR|-"); continue
  fi

  # ---- Slurm view of the run job
  st=; ex=; el=; nd=; start=; end=; so=
  if [[ $rid =~ ^[0-9]+$ ]]; then
    IFS='|' read -r st ex el nd start end < <($T 60 sacct -j "$rid" -X -n -P \
      -o State,ExitCode,Elapsed,NodeList,Start,End 2>/dev/null | head -1)
    so=$($T 60 sacct -j "$rid" -X -n -P -o StdOut 2>/dev/null | head -1)
  fi
  echo "run job   : state=${st:-?} exit=${ex:-?} elapsed=${el:-?} node=${nd:-?} start=${start:-?} end=${end:-?}"

  # ---- case state
  echo "CaseStatus (last 4 run/archive events):"
  grep -E 'case.run|st_archive|case.submit' "$C/CaseStatus" 2>/dev/null | tail -4 | sed 's/^/   /'
  RD=$(xq "$C" RUNDIR); AR=$(xq "$C" DOUT_S_ROOT)
  rs=$(xq "$C" RESUBMIT); cr=$(xq "$C" CONTINUE_RUN); sn=$(xq "$C" STOP_N); wc=$(xq "$C" JOB_WALLCLOCK_TIME)
  latest=$($T 60 find "$AR/rest" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort | tail -1)
  rpf=$($T 30 find "$RD" -maxdepth 1 -name 'rpointer.lnd*' -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1 | cut -d' ' -f2-)
  rp=$([ -n "$rpf" ] && head -1 "$rpf" | awk '{print $1}')
  echo "settings  : RESUBMIT=${rs:-?} CONTINUE_RUN=${cr:-?} STOP_N=${sn:-?} wallclock=${wc:-?}"
  echo "restarts  : latest archived=${latest:-none} | rpointer.lnd -> ${rp:-missing}"
  echo "RUNDIR    : ${RD:-?}"

  # ---- batch job log (sacct StdOut pattern, else any file naming the job id)
  jl=
  if [ -n "$so" ]; then
    jl=${so//%j/$rid}; jl=${jl//%u/$U}; jl=${jl//%x/run.$c}; jl=${jl//%A/$rid}
    [ -f "$jl" ] || jl=
  fi
  if [ -z "$jl" ] && [[ $rid =~ ^[0-9]+$ ]]; then
    jl=$($T 60 find "$C" "$RD" -maxdepth 1 -type f -name "*$rid*" 2>/dev/null | head -1)
  fi
  hr; echo "batch job log: ${jl:-not found (sacct StdOut=${so:-empty})}"
  [ -n "$jl" ] && $T 20 tail -n 25 "$jl" | sed 's/^/   /'

  # ---- model logs written by this run
  logs=
  if [ -n "$RD" ] && [ -d "$RD" ]; then
    since=${start:-}; [[ $since =~ ^[0-9]{4}- ]] || since="$(date -d '-2 days' '+%Y-%m-%d %H:%M')"
    since=${since/T/ }
    logs=$($T 60 find "$RD" -maxdepth 1 -type f \( -name 'cesm.log.*' -o -name 'lnd.log.*' -o -name 'drv.log.*' -o -name 'med.log.*' -o -name 'atm.log.*' \) \
           -newermt "$since" -printf '%T@ %p\n' 2>/dev/null | sort -n | cut -d' ' -f2-)
  fi
  hr
  if [ -z "$logs" ]; then
    echo "model logs: none written since the run started (the model never got going)"
  else
    for f in $logs; do
      echo "== $(basename "$f")  ($(stat -c %s "$f" 2>/dev/null) bytes)"
      case "$f" in *.gz) rd_cmd="zcat";; *) rd_cmd="cat";; esac
      echo "   error lines:"
      $T 60 $rd_cmd "$f" 2>/dev/null | grep -nE "$ERR_RE" | grep -viE 'error_?(code|stat)? *= *0|errors? *: *0|no error' | tail -12 | sed 's/^/     /'
      case "$f" in */cesm.log.*|*/lnd.log.*)
        echo "   tail:"; $T 60 $rd_cmd "$f" 2>/dev/null | tail -n 15 | sed 's/^/     /';;
      esac
    done
  fi

  # ---- verdict
  alltext=$( { [ -n "$jl" ] && $T 20 tail -n 200 "$jl"; for f in $logs; do $T 60 tail -n 400 "$f"; done; } 2>/dev/null )
  secs=$(awk -F'[-:]' '{ if (NF==4) print $1*86400+$2*3600+$3*60+$4; else if (NF==3) print $1*3600+$2*60+$3; else print 0 }' <<<"${el:-0:0:0}")
  if [[ $st == NODE_FAIL* || $st == BOOT_FAIL* ]]; then
    verdict=NODE_FAILURE; next="infrastructure: bash resume_failed_segment.sh $c (check), then --submit"
  elif [[ $st == TIMEOUT* ]] || grep -q 'DUE TO TIME LIMIT' <<<"$alltext"; then
    verdict=WALLCLOCK_TIMEOUT; next="segment did not fit in ${wc:-?}: slow node or slow site; resume, and raise JOB_WALLCLOCK_TIME if it repeats"
  elif [[ $st == CANCELLED* ]]; then
    verdict=CANCELLED; next="someone/something cancelled it: confirm it was not intended, then resume_failed_segment.sh"
  elif [[ $st == OUT_OF_ME* ]] || grep -qiE 'out of memory|oom-kill' <<<"$alltext"; then
    verdict=OUT_OF_MEMORY; next="memory: resume once; if it repeats, look at the case's memory/PE layout"
  elif grep -q 'user_xml_dir .*does not exist' <<<"$alltext"; then
    verdict=SOURCEMODS_DANGLING; next="python3 ~/maintenance/scripts/check_sourcemods.py (see docs/SOURCEMODS_DANGLING_LINKS.md)"
  elif grep -q 'build-namelist failed\|CLMBuildNamelist' <<<"$alltext"; then
    verdict=NAMELIST_REGENERATED; next="FATES AD cases must be submitted with --skip-preview-namelist"
  elif grep -qE 'ENDRUN|MPI_ABORT|forrtl|SIGSEGV|[Ss]egmentation|[Ff]loating[ -][Pp]oint|NaN' <<<"$alltext"; then
    verdict=MODEL_ERROR; next="real model error: do NOT just resubmit; read the error lines above (same restart -> same crash)"
  elif [ -z "$logs" ] && [ "${secs:-0}" -lt 120 ]; then
    verdict=START_FAILURE; next="failed before the model wrote logs: read the batch job log; transient (filesystem/MPI) if it is empty"
  elif [[ $st == FAILED* ]]; then
    verdict=FAILED_UNCLASSIFIED; next="no known pattern: read the log excerpts above"
  fi
  if [ -n "$latest" ] && [ -n "$rp" ] && ! grep -q "$latest" <<<"$rp"; then
    next="$next | NOTE rpointer ($rp) is not the latest archived restart ($latest): check before resuming"
  fi
  hr; echo "VERDICT   : $verdict"; echo "NEXT      : $next"
  SUMMARY+=("$c|$verdict|$next")
done

echo; echo "======================== SUMMARY ========================"
for s in "${SUMMARY[@]}"; do IFS='|' read -r c v n <<<"$s"; printf '%-22s %s\n   -> %s\n' "$v" "$c" "$n"; done
echo "READ ONLY: nothing was cancelled, submitted or changed."
