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

## One-shot for remaining sites (held cases)

`scripts/offload_legacy_sites.sh SITE...` runs plan → archive → clean for the legacy cases at the
given sites. A dependant at the same site that is not FIXNC/OBSPFT (e.g. `DK-Sor_FATES_HIST1996_2024_*`)
is added with `--extra-case` and is archived and deleted with the case it depends on. Any other hold
(live job, Git lock, recent writes, a FIXNC or other-site dependant) stops the script before anything
is written. It deletes only when every planned case is `ARCHIVED_VERIFIED`. Logs go to `~/legacy1000_logs/`.

```bash
cd ~/maintenance_tools/legacy1000
curl -fsSL -o offload_legacy_sites.sh https://raw.githubusercontent.com/lyanyeung/maintenance/main/scripts/offload_legacy_sites.sh
bash offload_legacy_sites.sh DK-Sor IT-Noe
```

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

**2026-11-02/03, login6001/6002 (operator output).**
Session `/research/xclimatetbm/ly3n24/Scratch_Offload/legacy1000_20261102T230224Z_c043d55e`.

- archive: `ARCHIVE_DONE verified=56 skipped=0`, NEED_GB≈280.8 (DEST_FREE_GB 1952.5, NFS
  `xclimatetbm.files.soton.ac.uk:/ifs/research/FHS/FHSallocated/xclimatetbm`).
- clean: pilot `AT-Neu_FATES_SPINUP500_11P` first, then the rest:
  `CLEAN_DONE unlinked=185848 rmdir=10588 kept_changed=0` (55 cases, all `roots_left=0`; AT-Neu
  reported `CLEAN_SKIPPED` because the pilot had already removed it).
- `myfiles` after: Scratch 564/2000 GB, 702,900/1,228,800 files.
- The four batch-6 continuation cases (DE-Tha, NL-Loo, IL-Yat, RU-Fyo) reported
  `not_copied_beyond1000=0`.
- Held on Scratch: `DK-Sor_FATES_SPINUP500` and `IT-Noe_FATES_SPINUP500_11P`.

**2026-11-03 06:50, `offload_legacy_sites.sh DK-Sor`.** Session
`/research/xclimatetbm/ly3n24/Scratch_Offload/legacy1000_20261103T065003Z_e803a9ea`.
`DK-Sor_FATES_SPINUP500` (0 h0a files left on Scratch, last restart 4421) and its dependants
`DK-Sor_FATES_HIST1996_2024_NOAA_CO2` and `DK-Sor_FATES_HIST1996_2024_TEST`: verified=3 skipped=0,
`CLEAN_DONE unlinked=8934 rmdir=405 kept_changed=0`. `myfiles` after: 552/2000 GB, 698,352/1,228,800 files.

**Bug found in v1 (`20261102-legacy1000-v1`), fixed in v2.** The plan stored RUNDIR/DOUT_S_ROOT as
written in env (`/scratch/...`), but the walked roots were resolved (`/iridisfs/scratch/...`), so
the beyond-1000 rule never matched. In both sessions above, the batch-6 output beyond 1000
years (DE-Tha, NL-Loo, IL-Yat, RU-Fyo, DK-Sor) was therefore **copied** into the tars and then deleted
from Scratch, not discarded. Scratch result is as intended; the Research Filestore holds
more than intended. v2 stores real paths.

**v2 also:** a `RUN_REFCASE`/`RUN_REFDATE`/`RUN_REFDIR` entry of a `RUN_TYPE=startup` case is not a
dependency (CIME ignores it; it is left over from create_clone). Example:
`IT-Noe_B02_FIXNC_AD400_BARE34_SHRUB66_AD5` (startup, EXEROOT = shared GF-Guy bld) names
`IT-Noe_FATES_SPINUP500_11P` only as RUN_REFCASE. Hybrid/branch references still hold.
