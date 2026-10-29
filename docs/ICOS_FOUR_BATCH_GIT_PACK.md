# Four ICOS/FLUXNET batches — CIME Git inode packing (IRIDIS6)

This is the maintenance-repository copy of the existing, tested
[38-case tool](https://github.com/lyanyeung/CLM-FATES-single-site-workflows/blob/main/maintenance/cime_git_cleanup/pack_four_batch_case_git.py).
The Python source was copied **unchanged** from the upstream Git blob
`4ea7fa118669028e0da074b58ffe6646aba37add` (29 October 2026).
It does not cover the separate AmeriFlux AD400/NORMAL400 cases.

**Scope:** exactly 38 CIME case Git repositories from four experiments.
Do not infer current run completion, eligibility or idle status from a case name.

| Campaign | Case count | Git repository pattern |
| --- | ---: | --- |
| FLUXNET_MultiSite_6 | 6 | `<SITE>_FATES_SPINUP500` (includes extended 3000-year runs) |
| FLUXNET_MultiSite_8 | 8 | `<SITE>_FATES_SPINUP500` |
| FLUXNET_MultiSite_11 | 11 | `<SITE>_FATES_SPINUP500_11P` except two overrides |
| FLUXNET_MultiSite_13 | 13 | `<SITE>_FATES_SPINUP1000_S30SHARED` |

Case overrides: `IT-Noe_ERA5_ABW_BRANCH2009` and
`CH-Aws_FATES_SPINUP1000_RH42A_V8_CONT2016`.
Before maintenance, verify these are still the desired current CIME case variants
against the live four-batch monitor and the local directories.

## Critical restrictions

- **Do not run concurrently** with the 29 October AmeriFlux CIME Git-packing
  job `gc_finished_nc` (initial Job ID `4757113`) or another Git-maintenance job.
  The tools share a Home advisory lock; even a second queued job is unnecessary.
- The audit is read-only. `run` changes **only per-case Git metadata** using
  `git gc --no-prune`; it does not delete case folders, NetCDF outputs,
  histories, restarts, scientific inputs, or Git repositories.
- `run` performs queue, case-root, lock, Git connectivity, snapshot and
  post-pack object validation. Ambiguous Slurm jobs, shared repositories,
  failed checks, and newly appearing locks stop packing.
- `--skip-locked` permits **only already-verified unlocked** case repositories
  to proceed; it does not remove or override `gc.pid` / `*.lock`.
  A lock-free case is **not necessarily idle**: interactive/remote writers and
  dynamically named jobs must be checked separately.
- The original 26 October audit found **17 locked cases**, with nine cases
  already packed. These are historical observations, **not** current status.
  Do not assume all 38 will free inode slots. Verify live results with `myfiles`.
- GitHub hosts only code and documentation. No user data, archives,
  manifests, logs or simulation outputs should be committed.

## 1. Prepare the script on a LOGIN node

```bash
if test -d "$HOME/maintenance/.git"; then
  git -C "$HOME/maintenance" pull --ff-only
else
  git clone https://github.com/lyanyeung/maintenance.git "$HOME/maintenance"
fi
TOOL="$HOME/maintenance/scripts/pack_four_batch_case_git.py"
test -s "$TOOL" && python3 -B "$TOOL" --version
```

This network access happens only on the login node.
The red-node Slurm job reads the already downloaded Python script from Home,
local CIME Git repositories on Scratch, and Slurm services; no internet access
or GitHub authentication is needed inside the job.

## 2. Inspect current queues / locks (READ ONLY)

```bash
squeue -u "$USER" -o "%.12i %.2t %.10M %.38j %R"
python3 -B "$HOME/maintenance/scripts/pack_four_batch_case_git.py" locks
```

The lock report can be comparatively expensive if several Git directories are
large; if concerned about login-node I/O, execute `locks` on a single Slurm CPU
instead, as with the full audit below. Never remove Git locks automatically.

## 3. Submit a read-only AUDIT on a RED compute node

Only after the AmeriFlux packer is no longer running:

```bash
TOOL="$HOME/maintenance/scripts/pack_four_batch_case_git.py"
PYEXE="$(command -v python3)"
sbatch --partition=batch --job-name=gc_icos_audit \
  --nodes=1 --ntasks=1 --cpus-per-task=1 --mem=4G --time=01:00:00 \
  --output="$HOME/gc_icos_audit_%j.log" \
  --wrap="'$PYEXE' -B '$TOOL' audit"
```

Review the output `AUDIT PASSED` (or `AUDIT FAILED` with exact locked/missing
cases), per-case `GIT_INODES`, `LOOSE`, and Slurm-job relations.
The audit is not a completion/convergence assessment of any simulation.

## 4. Submit the actual pack job ONLY at an idle case checkpoint

After reviewing audit and ensuring target case roots have no writers:

```bash
TOOL="$HOME/maintenance/scripts/pack_four_batch_case_git.py"
PYEXE="$(command -v python3)"
sbatch --partition=batch --job-name=gc_icos_38_pack \
  --nodes=1 --ntasks=1 --cpus-per-task=1 --mem=4G --time=02:00:00 \
  --output="$HOME/gc_icos_38_pack_%j.log" \
  --wrap="'$PYEXE' -B '$TOOL' run --skip-locked"
```

If some experiments are active, restrict to a **verified idle batch** with
`--batch 6`, `--batch 8`, `--batch 11`, or `--batch 13` appended
**inside** the `--wrap` command. For a small exact subset, repeat
`--case EXACT_CIME_CASE_NAME`; the script accepts only its 38-case whitelist.
Do not use `--skip-locked` as an override for activity or missing case health.

If Slurm reports `BLOCK_UNKNOWN_JOB` for a simultaneous unrelated job,
this is a safe refusal; investigate rather than weakening protections.
Do not use `git gc --force`, `rm -rf .git`, or `git gc --prune=now`.

## 5. Check actual results

```bash
# Replace JOBID with the Slurm job number
grep -E '^(BEFORE|AFTER|SKIP_LOCKED|COMPLETE|PARTIAL|STOP)' \
  "$HOME/gc_icos_38_pack_JOBID.log" | tail -35
myfiles
```

`COMPLETE` reports before/after Git inode counts of successfully packed cases.
`PARTIAL` reports skipped locked cases. Real Scratch quota is established
by `myfiles`; concurrently growing model outputs can offset the savings.
Previously packed cases may show `LOOSE=0` and little or no further benefit.

See the [upstream four-batch runbook](https://github.com/lyanyeung/CLM-FATES-single-site-workflows/blob/main/maintenance/cime_git_cleanup/FOUR_BATCH_38_CASES.md)
for exact case isolation, snapshot verification and historical observations.
