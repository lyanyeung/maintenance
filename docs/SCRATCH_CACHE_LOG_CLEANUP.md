# Scratch surface ERA cache and old log cleanup

`scripts/scratch_cache_log_cleanup.py` is a separate workflow from Home backup
and FLUXNET DD/MM/YY cleanup. It needs Python 3.9+ standard library, `squeue`,
`scontrol` and `findmnt`. No Conda activation, model submission, or Git pull is
required. It does not connect to IRIDIS remotely; run it there as `ly3n24`.

## Scope

The operator's 27 October 2026 snapshot showed Scratch at **846/2000 GB** and
**1,011,142/1,228,800 files**, leaving **217,658 file slots**. These are
user-reported historical values, not live telemetry or a cleanup result.

| File group | Treatment |
| --- | --- |
| ERA5/ERA5-Land/ERA-Interim `.nc`, `.nc4`, `.grib`, `.grb`, `.grb2` files under a site's `subset_data/` | Candidate only when at least seven days old, that site's configured `fsurdat` exists with a NetCDF/HDF header, no detected configuration/source/receipt reference, and no related queued/running job |
| `*.log`, `*.log.*`, `*.out`, `*.err` and gzip variants | Candidate only when at least seven days old and unreferenced/inactive, in the explicit locations below |
| Site original `forcing/`, raw packages, companion ERA HH CSV, processed forcing and all `experiments/` files, including downloaded external evidence | Always retained; scanned metadata is read-only |
| Surface/domain/mesh/PFT/soil/land-use data, CIME XML/namelists, `CaseStatus`, scientific receipts, source code, Git, active model run/build directories, model history and restarts | Never deletion candidates |

Old-log locations: Home top level; the immediate contents of Home directories
starting `ameriflux_`, `AmeriFlux_`, `Batch`, `CLM_FATES_maintenance`; site top level
and `subset_data`; case top level and `logs/`; and
`CTSM_FATES_OUTPUTS/archive/<case>/logs/`. No recursive deletion of arbitrary
project directories and no removal of empty directories is performed.

**A surface file is not evidence that meteorological repair is complete.** In
particular, AmeriFlux Batch03's source ERA5/ERA5-Land evidence remains protected,
including the US-Me6 and US-MOz work. Configured `fsurdat` is a structural
prerequisite for considering an unused **surface-generated meteorological
cache**; it is not a scientific PFT/surface approval or a forcing-readiness gate.
If no qualifying cache exists, the ERA candidate count is zero. There is no
fallback that broadens deletion into original forcing or `external_evidence`.

## Audit and cleanup

Download the script from a **specific verified commit**, as supplied in the
handoff command, and verify its SHA256. With `SCRIPT` set to that file:

```bash
# Read-only inventory; no reports or archives are written.
python3 -B "$SCRIPT" audit

# Include the retention reason for every discovered file.
python3 -B "$SCRIPT" audit --details

# Archive eligible files, verify, recheck dependencies and remove unchanged originals.
python3 -B "$SCRIPT" clean --confirm ARCHIVE_VERIFY_CLEAN_OLD_CACHE_LOGS
myfiles
```

`--min-days 14` increases retention. Values below seven days are rejected.
Any unresolved Slurm query error stops the operation instead of interpreting
an unavailable queue as an idle system. Full job metadata protects pending as
well as running jobs, their working directories and stdout/stderr. Related
case and site paths are excluded. No job is cancelled or altered.

The reference scan reads source/configuration/receipt metadata under CIME
cases, experiments, sites, the two source repositories and selected Home script
directories. It checks exact paths, relative basenames and ERA stream directory
references. Large metadata files stream in chunks. Reports list why a file is
kept. **Literal-reference scanning cannot prove the absence of every future or
dynamically constructed reader.** This is why source/evidence zones are excluded
and every eligible file must have a verified, restorable backup before removal.

## Backup verification and receipts

Clean mode first requires the effective destination to be mounted as NFS:

`/research/xclimatetbm/ly3n24/Scratch_Declutter/`

If the mount is unavailable or not NFS, no cleanup occurs. A maintenance lock
prevents simultaneous runs. Each UTC-stamped session contains a compressed TAR,
`manifest.json` and an append-only `cleanup_actions.jsonl`. Research data and
these private manifests are never uploaded to GitHub.

Each source's identity, size and nanosecond modification/change times are
recorded; every archived member is verified against its source SHA256. The TAR
also receives a SHA256. Files changed while being archived cause a stop before
deletion. A fresh dependency/Slurm audit runs after backup, and the archive is
verified again before unlinking. New references, new jobs and changed sources
are retained. The queue refreshes during deletion as well. Symlinks, symlink
ancestors, hard links and files owned by another user are excluded.

`VERIFIED_BACKUP <path>` proves the archive check completed; only
`CLEAN_COMPLETE removed_files=...` reports removed originals. The GiB value is
the sum of nominal file sizes; actual quota changes must be checked with
`myfiles`. A GitHub commit or local test pass does not prove any IRIDIS file
was removed or that any particular number of files can be reclaimed.

To verify an existing session separately:

```bash
python3 -B "$SCRIPT" verify --session /research/xclimatetbm/ly3n24/Scratch_Declutter/<actual_session>
```

To restore, verify first, then extract into a new review directory rather than
overwriting live paths:

```bash
mkdir -p "$HOME/scratch_cleanup_restore_review"
tar -xzf /research/xclimatetbm/ly3n24/Scratch_Declutter/<actual_session>/files.tar.gz \
  -C "$HOME/scratch_cleanup_restore_review"
```

Member names contain original absolute paths with the leading slash removed.
Inspect the manifest and restored files before copying a particular member back
to its original location. Do not blanket-extract over a live Scratch tree.

## Tests and separate inode work

```bash
python3 -B -m unittest discover -s tests -v
```

Regression tests exercise queued-job/stream/receipt protection, protected source
zones, valid archive/deletion/restore, corrupt backups, changed sources and
symlink/hard-link exclusions. They use temporary fixtures; neither Slurm nor
real research data are altered.

If this audit finds few candidates, identify inode-heavy directories separately.
Per-case Git packing is an existing, distinct workflow in
[CLM-FATES-single-site-workflows/maintenance/cime_git_cleanup](https://github.com/lyanyeung/CLM-FATES-single-site-workflows/tree/main/maintenance/cime_git_cleanup).
It is not performed by this script, and requires its own idle-case checks.
