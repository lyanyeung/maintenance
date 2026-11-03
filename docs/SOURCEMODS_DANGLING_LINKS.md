# Dangling `SourceMods` links: case.run fails in 2–3 s (3 Nov 2026)

[Check/repair tool](../scripts/check_sourcemods.py) · fixes in
[offload_legacy_spinup1000.py](../scripts/offload_legacy_spinup1000.py) and
[offload_failed_cases.py](../scripts/offload_failed_cases.py) (`case_symlink_hops`)

## Symptom

- 22 `case.run` jobs submitted 2026-11-03 06:19–06:44 (needleleaf variant-F `*_NLF` reruns and
  ICOS Batch04 `*_DIRECT` cases) ended `FAILED 1:0` after 2–4 s. Their `st_archive` jobs stayed
  `PD (DependencyNeverSatisfied)`.
- Every job output ended with
  `ERROR: user_xml_dir <case>/SourceMods/src.drv does not exist`
  (raised by the driver `buildnml` while `case.run` regenerates namelists, before the model starts;
  no `cesm.log` is written, so `ls -t $RUNDIR/cesm.log.* | xargs tail` waits on stdin).
- A scan of `CLM_FATES/cases` found ≈208 FIXNC cases (all batches, AD400 and NORMAL400, DIAG/FIX
  trials) whose `SourceMods` is not a directory: `ls <case>/SourceMods` prints the path itself,
  i.e. a symlink whose target no longer exists.

## Cause

`create_clone --keepexe` makes `<clone>/SourceMods` a symlink to the **source case's**
`SourceMods` (already seen once: Batch02 BE-Bra, Non-Competition-CLM-experiment
`examples/Batch02_10/README.md`). The FIXNC cases were cloned from the legacy
`<SITE>_FATES_SPINUP500/1000*` cases. `offload_legacy_spinup1000.py` (v1/v2, sessions of
2026-11-02 23:02 and 2026-11-03 06:50/06:56/07:05) deleted those case folders. Its dependency
check looked at `env_*.xml`, `user_nl_*`, `CaseDocs` and RUNDIR symlinks, **not at symlinks in a
remaining case's folder**, and the clone source's name does not have to appear in any of those
files. So the clone sources were not held, and every clone's `SourceMods` link was left dangling.

To confirm per case: the `link_target` column of `check_sourcemods.py` should name a deleted
`*_FATES_SPINUP*` folder.

Consequences: any FIXNC case fails at its next `case.run`, including running chains at their next
`RESUBMIT` segment. The executables themselves are not affected (shared EXEROOTs were held by the
EXEROOT reference).

## Repair

Do not `mkdir -p <case>/SourceMods/src.drv`: through a dangling link that recreates a stub of the
deleted case at its old path.

```bash
cd ~/maintenance && git pull
# 1. read-only report (one row per case, summary, active jobs in broken cases)
python3 scripts/check_sourcemods.py > ~/sourcemods_report.tsv
sed -n '/== SUMMARY/,$p' ~/sourcemods_report.tsv
# 2. relink dangling links to the executable-owner case's SourceMods (logged to ~/sourcemods_relink_<UTC>.tsv)
python3 scripts/check_sourcemods.py --fix --confirm RELINK_SOURCEMODS
# 3. report again: expect no DANGLING rows
python3 scripts/check_sourcemods.py | sed -n '/== SUMMARY/,$p'
```

`--fix` changes only cases whose `SourceMods` is a dangling symlink and whose executable owner
(`cases/<EXEROOT parent>/SourceMods`) resolves to a real directory containing `src.drv`. This is
the same target rule as Batch02 `44_*` and `Cross_cohort_NORMAL400/01_*`: the SourceMods that
built the executable the case actually runs.

Rows marked `NEED_RESTORE` (own build, own SourceMods lost) or `NEED_REVIEW` are not touched.
Restore their `SourceMods` from the offload tar of the deleted source case, then point the link at
the restored copy or replace it with a real directory:

```bash
SES=/research/xclimatetbm/ly3n24/Scratch_Offload/legacy1000_<UTC>_<id>
mkdir -p ~/restore_review
tar -xf "$SES/<SOURCE_CASE>.tar" -C ~/restore_review --wildcards '*/<SOURCE_CASE>/SourceMods*'
```

Phenology-patched cases must have their own build and a real `SourceMods/src.clm/EDPhysiologyMod.F90`
(Non-Competition-CLM-experiment `maintenance/RULES_FOR_NEW_CASES.md`); the report's
`src.clm_patch` column shows whether that file is visible.

After the repair: `scancel` the orphan `st_archive` jobs and `./case.submit` the 22 failed cases.

## Prevention (v3, 2026-11-03)

`offload_failed_cases.case_symlink_hops(case)` lists every hop of every top-level symlink in a
case folder (`C/SourceMods -> B/SourceMods -> A/SourceMods` gives B and A; deleting either breaks
C). Both offload tools now hold a case if any remaining case's folder link passes through it, at
plan time and again in the pre-archive/pre-clean recheck. Regression tests:
`tests/test_offload_legacy_spinup1000.py::test_sourcemods_link_holds_clone_source`,
`test_symlink_hops_see_intermediate_case`, and `tests/test_check_sourcemods.py`.

## Run record

- 2026-11-03 13:0x: diagnosis from operator output (sacct + job logs + `SourceMods` scan).
- Repair: _to be filled from `~/sourcemods_relink_<UTC>.tsv` and the post-fix summary._
