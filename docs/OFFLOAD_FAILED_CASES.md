# Offload failed first-run FIXNC cases from Scratch to the Research Filestore

[Python tool](../scripts/offload_failed_cases.py) (needs `audit_pack_fixnc_case_git.py` in the same folder).
Python 3.9+, no third-party modules.

## What moves, what stays

**Moves (archived, then removed from Scratch):** `run`, `bld` (`$CIME_OUTPUT_ROOT/<case>`) and
`$DOUT_S_ROOT` (`archive/<case>`: histories, restarts, logs) of the **first production runs**
(AD400, NORMAL400, AD5 smoke, superseded input variants) at sites whose first run clearly failed
(`docs/NEEDLELEAF_COLLAPSE_DIAGNOSIS.md`, sections 10, 14 and 16 of Non-Competition-CLM-experiment):

| Group | Sites |
| --- | --- |
| Needleleaf collapse | FI-Sod CH-Dav IL-Yat DE-Obe IT-Ren CZ-BK1 CA-Ca3 CA-LP1 US-NC2 US-GLE FI-Hyy |
| Needleleaf part lost | US-Ho2 DE-Tha RU-Fyo US-Ho1 BE-Bra US-Ho3 FR-FBn FI-Let US-SP1 US-NR1 BE-Vie US-Me2 IT-Lav US-Me6 CA-TP4 |
| Phenology | CZ-Stn DE-Lnf US-SRM US-SRG FR-Pue US-CMW |

Override with `--sites ...` or `--exclude-site ...`.

**Stays on Scratch:**
- **Every site in a current experiment, as a whole site**: any site with a trial case
  (`*_DIAG<n>`, `*_FIX<X><n>`, `*_PHENFIX<n>`) or any ACTIVE / RECENT / Git-locked case. Its
  original AD400 is the trial control and the clone source, so it stays too.
- **Any case that a remaining case still depends on.** This means its name or output path appears in
  that case's `env_*.xml`, `user_nl_*` or `CaseDocs` (`--keepexe` EXEROOT, RUN_REFDIR, `finidat`),
  or one of that case's RUNDIR symlinks points into it.
- **The DE-Tha / NL-Loo protected examples.**
- **Case directories.** They are archived as well, but they stay on Scratch for `create_clone`.
  A clone made from a case whose `bld` was removed must be built again.
- **The legacy 6/8/11/13 SPINUP cases and other experiments.**

## Steps

```bash
git -C ~/maintenance pull --ff-only
TOOL="$HOME/maintenance/scripts/offload_failed_cases.py"
PYEXE="$(command -v python3)"

# 1. READ ONLY: plan (single Slurm CPU, or login node)
sbatch --partition=batch --job-name=offload_plan --nodes=1 --ntasks=1 --cpus-per-task=1 \
  --mem=4G --time=01:00:00 --output="$HOME/offload_plan_%j.log" \
  --wrap="'$PYEXE' -B '$TOOL' plan"
LOG=$(ls -t ~/offload_plan_*.log | head -1)
grep -E '^(  |OFFLOAD|TOTAL|PLAN_WRITTEN)' "$LOG" | head -80; grep ^HELD "$LOG"
```

Review the `OFFLOAD` rows (sites, cases, GB) and the `HELD` reasons. The plan JSON is in `~/offload_plans/`.

```bash
# 2. Where is /research mounted? (must report nfs/nfs4)
"$PYEXE" -B "$TOOL" probe-mount
```

Run `archive` and `clean` on a node where `probe-mount` shows NFS. If compute nodes do not mount
`/research`, use a login node inside `tmux`/`screen`, because a large run takes hours:

```bash
# 3. Archive + verify (writes to /research/xclimatetbm/ly3n24/Scratch_Offload/offload_<UTC>_<id>/)
PLAN=$(ls -t ~/offload_plans/offload_plan_*.json | head -1)
nice -n 10 "$PYEXE" -B "$TOOL" archive --plan "$PLAN" 2>&1 | tee ~/offload_archive_$(date +%Y%m%d_%H%M).log
```

Each case gets `<case>.tar`, `<case>.manifest.tsv` (every file: size, mtime, SHA256 recorded
while reading) and `<case>.VERIFIED`. The receipt is written only after the tar has been read back
from the Research Filestore and every member matched. A case that is busy or changed is skipped.
**Nothing is deleted in this step.**

```bash
# 4. Delete from Scratch only what was verified and has not changed since
SES=/research/xclimatetbm/ly3n24/Scratch_Offload/offload_<UTC>_<id>
"$PYEXE" -B "$TOOL" clean --session "$SES" --confirm DELETE_VERIFIED_SCRATCH_OUTPUTS
myfiles
```

`clean` checks again that each case is idle, that no trial case has appeared at the site and that
no remaining case depends on it. It then unlinks only files whose size and mtime equal the manifest.
Files that changed or are new stay and are reported as `kept_changed`. Directories are removed only
when empty. Every action is logged in `$SES/cleanup_actions.tsv`. Use `--case NAME` to do one case first.

```bash
# Optional: re-verify a session at any time (read only)
"$PYEXE" -B "$TOOL" verify --session "$SES"
```

## Restore

The tar members use absolute paths without the leading `/`:

```bash
mkdir -p ~/restore_review && tar -xf "$SES/<case>.tar" -C ~/restore_review
# files appear under ~/restore_review/iridisfs/scratch/ly3n24/...
```

To put a case back in place, extract it with `-C /` only after checking that nothing exists at those
paths.
