# Offload the legacy 1000-year Normal spin-ups (ICOS/FLUXNET 6/8/11/13) from Scratch

[Python tool](../scripts/offload_legacy_spinup1000.py). It needs `offload_failed_cases.py` and
`audit_pack_fixnc_case_git.py` in the same folder. Python 3.9+, no third-party modules.

Operator decision (2 November 2026): move every 1000-year Normal spin-up from
`CLM-FATES-single-site-workflows/examples/FLUXNET_MultiSite_{6,8,11,13}` to the Research
Filestore, then delete those cases from Scratch. Also delete from Scratch the output of the
first ICOS batch (FLUXNET_MultiSite_6) beyond 1000 model years. That output was the 1000→3000
continuation of RU-Fyo, DE-Tha, DK-Sor, NL-Loo and IL-Yat.

## What is selected

The cases at the 38 legacy sites whose name is `<SITE>_FATES_SPINUP500…`, `<SITE>_FATES_SPINUP1000…` or
`IT-Noe_ERA5_ABW_BRANCH<yyyy>`. This includes `*_SPINUP500`, `*_SPINUP500_11P`, `*_SPINUP1000_S30SHARED`, the superseded
`*_S30EXCL` and the CH-Aws RH* trials. FIXNC cases (AD400/NORMAL400, DIAG/FIX/PHENFIX) and the
`*_OBSPFT_NC` protected examples are **never** selected. `--extra-case NAME` adds an exact case,
for example `DK-Sor_FATES_HIST1996_2024_TEST`, which restarts from the DK-Sor spin-up.

| Part of the case | Research Filestore (tar) | Deleted from Scratch |
| --- | --- | --- |
| case dir (`CLM_FATES/cases/<case>`) | yes | yes (`--keep-case-dirs` keeps it) |
| case-owned `bld` | yes | yes |
| shared EXEROOT (path does not name the case) | no | **never** |
| run dir | yes, unless the case ran past 1000 years | yes |
| archive histories, model year ≤ start+999 | yes | yes |
| archive restarts, year ≤ start+1000 (1000-year checkpoint kept) | yes | yes |
| archive logs | yes | yes |
| **> 1000 years** (histories > start+999, restarts > start+1000, run dir of a case past 1000 y) | **no** (`--copy-beyond-1000` at plan time copies it) | yes |

The start years are the RUN_START_YEAR table from `check_four_spinups.py`. Output beyond 1000
years is deleted without copying only when the case's `RUN_STARTDATE` year matches that table.
Otherwise it is copied.

## Held (left on Scratch, listed as `HELD`)

- A case with a related Slurm job, a Git lock, writes in the last 120 min or no CaseStatus.
- A case that any case staying on Scratch still references: name or output path in its `env_*.xml`,
  `user_nl_*` or `CaseDocs`, or a RUNDIR symlink into it. This is repeated until stable, so the
  source of an active branch stays too (CH-Aws RH34A while RH42A is active).
- A path that is a symlink, is not under Scratch, does not name the case, or overlaps another case.

`ORPHAN_OUTPUT` lists output dirs at legacy sites that have no case dir, for example
`DE-Hai_FATES_SPINUP500_backup`. They are only reported.

## Steps on IRIDIS6

```bash
# 0. Get the three files (no git pull needed)
D=~/maintenance_tools/legacy1000; mkdir -p $D; cd $D
for f in offload_legacy_spinup1000.py offload_failed_cases.py audit_pack_fixnc_case_git.py; do
  curl -fsSL -o $f https://raw.githubusercontent.com/lyanyeung/maintenance/main/scripts/$f || echo "FAILED $f"
done
python3 -B offload_legacy_spinup1000.py --version

# 1. READ ONLY plan: the table prints on screen
python3 -B offload_legacy_spinup1000.py plan 2>&1 | tee ~/legacy1000_plan.txt

# 2. READ ONLY: /research must be nfs/nfs4 on this node
python3 -B offload_legacy_spinup1000.py probe-mount
```

Check the `OFFLOAD` rows, the `HELD` reasons and `TOTAL`. If a case is held only because a
non-spin-up case depends on it, and that case can go too, run `plan` again with `--extra-case NAME`.

```bash
# 3. Archive + read-back verification, inside tmux (hours). Nothing is deleted.
tmux new -s legacy1000
cd ~/maintenance_tools/legacy1000
PLAN=$(ls -t ~/offload_plans/legacy1000_plan_*.json | head -1)
nice -n 10 python3 -B offload_legacy_spinup1000.py archive --plan "$PLAN" 2>&1 | tee ~/legacy1000_archive_$(date +%Y%m%d_%H%M).log
#   detach: Ctrl-b d      re-attach: tmux attach -t legacy1000
#   finished when the last line is: ARCHIVE_DONE session=... verified=N skipped=M
```

Each case gets `<case>.tar`, `<case>.manifest.tsv` (every file with size, mtime and SHA256;
beyond-1000 entries are marked `nocopy`) and `<case>.VERIFIED`. The receipt is written only after
the tar has been read back from the Research Filestore and every member has matched.

```bash
# 4. Delete from Scratch (only verified, unchanged files; case dir last)
SES=$(ls -dt /research/xclimatetbm/ly3n24/Scratch_Offload/legacy1000_* | head -1)
python3 -B offload_legacy_spinup1000.py clean --session "$SES" --confirm DELETE_VERIFIED_LEGACY_SPINUP1000 \
  2>&1 | tee ~/legacy1000_clean_$(date +%Y%m%d_%H%M).log
myfiles
```

`clean` checks every case again (idle, no new dependants) and unlinks only manifest files whose
size and mtime are unchanged. Every action is logged in `$SES/cleanup_actions.tsv`. Use
`--case NAME` to clean one case first, and `--rehash` to re-hash each tar before deleting.

## Restore

```bash
mkdir -p ~/restore_review && tar -xf "$SES/<case>.tar" -C ~/restore_review
# files appear under ~/restore_review/iridisfs/scratch/... and .../scratch/ly3n24/CTSM_FATES_OUTPUTS/...
```

## Note on earlier Research Filestore copies

The earlier rsync copies under `/research/xclimatetbm/ly3n24/CLM_FATES/Spinup_Archives/` from
`archive_eleven_site_1000yr.py` and `archive_completed_8_13.py` are not used or changed. After
this offload they are duplicates and can be reviewed separately.

## Run record

To be filled in from the IRIDIS6 output: plan totals, session path, verified/skipped,
CLEAN_DONE line and `myfiles` before/after. No IRIDIS run is claimed by this commit.
