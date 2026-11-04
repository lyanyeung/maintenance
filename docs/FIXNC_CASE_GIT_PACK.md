# FIXNC (no-competition) cases — find finished runs and pack CIME Git (IRIDIS6)

[Python tool](../scripts/audit_pack_fixnc_case_git.py) · Python 3.9+, no network, no third-party modules.

**Scope:** every case directory under `/iridisfs/scratch/ly3n24/CLM_FATES/cases`, classified by name:

| Cohort | Case-name pattern | Sites |
| --- | --- | --- |
| `ICOS_B01`–`ICOS_B04` | `<SITE>_B0N_FIXNC_...` | Non-Competition Batch01/02/03 (10 each), Batch04 (6) |
| `AMF_B01` | `<SITE>_AMF_FIXNC_...`, `<SITE>_AMF_B01SIX_FIXNC_...` | AmeriFlux Batch01 (BR-Ma2, CA-*, US-Aud, US-Bar) |
| `AMF_B02`–`AMF_B05` | `<SITE>_AMF_B0N_FIXNC_...` | AmeriFlux Batch02–05 |
| `LEGACY_6/8/11/13` | 38 old SPINUP cases | report only; use `pack_four_batch_case_git.py` |
| `PROTECTED_EXAMPLE` | `NL-Loo_OBSPFT_NC*`, `DE-Tha_OBSPFT_NC*` | report only unless named with `--case` |
| `OTHER` | anything else | report only unless named with `--case` |

## Why Git, and why only when finished

Recent CIME versions commit to the case `.git` at every `CaseStatus` event
(`case.run starting/success`, `st_archive starting/success`). A 400-year run in
80 five-year segments therefore leaves several hundred to a few thousand **loose
objects** (one inode each, plus up to 256 fan-out directories). `git gc --no-prune`
turns them into one pack (~40 inodes total per case). An active case keeps
creating new loose objects, so pack only after the run has ended.

## How "finished" is decided (read-only)

| State | Meaning | Packed by default? |
| --- | --- | --- |
| `DONE` | `RESUBMIT=0`, last `st_archive success`, newest `$DOUT_S_ROOT/rest/YYYY-…` minus `RUN_STARTDATE` year ≥ years in the name (AD400/NORMAL400→400, DIAG50/FIXA50→50, `…_AD5`→5) | **yes** |
| `STOPPED_SHORT` | ended cleanly but fewer years than the name says | only with `--include-idle` |
| `ENDED_NO_REST` | ended cleanly, no archived restart found | only with `--include-idle` |
| `STALLED` | last event success but `RESUBMIT>0` and no Slurm job (broken chain) | only with `--include-idle` |
| `ARCHIVE_PENDING` | model finished, `st_archive` never succeeded | only with `--include-idle` |
| `ERROR` / `INTERRUPTED` | last event error / `starting` with no job (timeout, kill) | only with `--include-idle` |
| `ACTIVE` | a Slurm job's name, WorkDir or command contains the case name | **never** |
| `RECENT` | `CaseStatus`/`.git` changed within `--quiet-minutes` (default 120) | **never** |
| `GIT_LOCKED` | `gc.pid` or `*.lock` present (locks are never removed) | **never** |

This is a bookkeeping check, not a convergence check.

## 1. Read-only audit (single Slurm CPU)

Do not overlap with `gc_finished_nc` or any other Git maintenance; the tool also
refuses `run` while `~/.cime_git_maintenance.lock` is held.

```bash
squeue -u "$USER" -o "%.12i %.2t %.10M %.45j %R"
TOOL="$HOME/maintenance/scripts/audit_pack_fixnc_case_git.py"
PYEXE="$(command -v python3)"
sbatch --partition=batch --job-name=gc_fixnc_audit \
  --nodes=1 --ntasks=1 --cpus-per-task=1 --mem=4G --time=01:00:00 \
  --output="$HOME/gc_fixnc_audit_%j.log" \
  --wrap="'$PYEXE' -B '$TOOL' audit"
```

Read the `COHORT SUMMARY` at the bottom (states, `.git` inodes, loose objects,
estimated inodes freed, number of `WOULD_PACK`), then the per-case table:

```bash
LOG=$(ls -t $HOME/gc_fixnc_audit_*.log | head -1)
sed -n '/^COHORT SUMMARY/,$p' "$LOG"
grep -P '^AMF_B0[35]\t' "$LOG" | cut -f2,3,8,9,10,11,13,16
grep UNATTRIBUTED_JOB "$LOG"
```

Optional `--measure` also counts entries in each case dir, `RUNDIR`, `bld`
(EXEROOT) and the archive — useful to see what else uses inodes. Read-only, slower.

## 2. Pack finished cases

AmeriFlux Batch03 and Batch05 first:

```bash
sbatch --partition=batch --job-name=gc_fixnc_pack \
  --nodes=1 --ntasks=1 --cpus-per-task=1 --mem=4G --time=04:00:00 \
  --output="$HOME/gc_fixnc_pack_%j.log" \
  --wrap="'$PYEXE' -B '$TOOL' run --cohort AMF_B03 --cohort AMF_B05"
```

Then all remaining `DONE` cases of all nine cohorts: drop the `--cohort` options.

- `run` stops if a Slurm job cannot be attributed to a case (`UNATTRIBUTED_JOB`,
  e.g. a controller). Check it; add `--allow-unknown-jobs` only if it cannot touch these cases.
- Every case is re-checked just before packing (queue, CaseStatus, recency,
  locks, `fsck --connectivity-only`), then snapshotted (HEAD, refs, reflogs,
  index, status, CaseStatus hash, full object-ID set). A difference after
  `gc` is a hard **STOP** for the whole run.
- Same Git options as the four-batch tool: reflogs and unreachable objects are kept;
  never `--force`, `--aggressive`, `--prune=now`, `rm -rf .git`.

```bash
LOG=$(ls -t $HOME/gc_fixnc_pack_*.log | head -1)
grep -E '^(AFTER|SKIP|COMPLETE|PARTIAL|STOP)' "$LOG"
myfiles
```

Nothing in this repository proves that cleanup ran on IRIDIS; use `myfiles`.

## Only the AD400 reruns (needleleaf `_NLF`/`_NLFP`, phenology `_PHEN`, US-Aud `_HINONE`)

`--name-regex` evaluates and packs only cases whose name matches, in any FIXNC cohort including
`ICOS_B04` and the DE-Tha example (never legacy). The NORMAL400 cases do not match and are not touched.

```bash
RX='_AD400.*_(NLF|NLFP|PHEN|HINONE)$'
"$PYEXE" -B "$TOOL" audit --name-regex "$RX"      # read only: which reruns finished AD400
sbatch ... --wrap="'$PYEXE' -B '$TOOL' run --name-regex '$RX'"
```

Packing changes only that AD case's `.git`. The NORMAL400 case reads the AD case's archived restart
(`RUN_REFDIR`) and its CaseDocs/user_nl/streams, and none of these change.
Cancelled `_NLF` cases (replaced by `_NLFP`) show as `STOPPED_SHORT`. Add `--include-idle` to pack them too.
