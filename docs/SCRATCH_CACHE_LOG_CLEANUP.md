# ERA5 cleanup: keep AmeriFlux Batch03 and Batch05

The operator's explicit 27 October 2026 policy supersedes the initial
surface-cache-only policy: **retain AmeriFlux Batch03 and Batch05 ERA5 /
ERA5-Land sources and shared dependencies; other batches' ERA5 sources and
caches may be archived and removed.** Old-log retention remains seven days.

`scripts/scratch_cache_log_cleanup.py` v2 requires Python 3.9+ standard library,
`squeue`, `scontrol` and `findmnt`. Run on IRIDIS6 as `ly3n24`; no Conda
activation, model submission or Git pull is required.

## Mount-check interruption on login6003

The operator's supplied output ended at `Research Filestore is not an NFS
mount; no archive or deletion`. The preceding `ELIGIBLE` lines were inventory,
not deletion receipts. The actual filesystem type was not reported by v2 and
must not be guessed from this error.

Version `20261027-v3-mount-probe` reads structured mount records, distinguishes
a mounted NFS filesystem from a shadowed autofs trigger, and reports the exact
mount target, source and filesystem type. Other filesystems still stop pending
review of the actual host's mount identity; this change does not assume CIFS,
GPFS or any other type is the user's backup destination.

Run `python3 -B "$SCRIPT" probe-mount` for a short read-only diagnostic. It
does not scan candidates, query Slurm, create reports or delete files. Clean
mode checks the mount before the long inventory. Default inventories now show
summaries; use `--details` for the full per-file list. Batch03/05 retention,
archive verification and deletion rules are unchanged.

## Explicit retention list

| Protected campaign | All ten protected station IDs |
| --- | --- |
| AmeriFlux Batch03 | US-Kon, US-Me2, US-Me6, US-MMS, US-MOz, US-Mpj, US-MtB, US-NC2, US-NR1, US-NR3 |
| AmeriFlux Batch05 | US-UiD, US-UMB, US-UMd, US-Var, US-Vcm, US-Vcp, US-WCr, US-Whs, US-Wjs, US-Wkg |

These lists were verified against the workflow repository's site manifests:
[Batch03](https://github.com/lyanyeung/CLM-FATES-single-site-workflows/blob/main/examples/AmeriFlux_MultiSite_10_Batch03/sites.tsv)
(Git blob `10cfac044cfe8d01d7a8033c3292cb3c2ee3e5c1`) and
[Batch05](https://github.com/lyanyeung/CLM-FATES-single-site-workflows/blob/main/examples/AmeriFlux_MultiSite_10_Batch05/sites.tsv)
(Git blob `27f138fdb1dd91c3d8ef8e4b70bb5f833643f9c3`).
The five AmeriFlux site manifests have no overlapping station IDs. Physical
file sharing can still occur, so retaining station folders alone is insufficient.

Protection covers ERA files in either batch's experiment tree, any of these
station folders, and ERA filenames containing their station IDs. Alternate
`AmeriFlux_Batch03_10` / `AmeriFlux_Batch05_10` names and corresponding Home
script directory names are recognised. Case is ignored for retention matching.
ERA files elsewhere are retained if referenced by these batches' source or
receipt metadata, including literal transitive receipt links, or reached through
their symlinks. Hard-linked files are excluded from cleanup.

## What the v2 policy changes

| File group | Treatment |
| --- | --- |
| Batch03 / Batch05 ERA5 and ERA5-Land, plus detected shared dependencies | Keep unconditionally, regardless of age or surface status |
| Other batches' ERA5 / ERA5-Land source downloads, unused surface meteorological caches and standalone original ERA5 HH companions | Archive, verify and remove unless protected by runtime/queued-job dependencies |
| A historical receipt from another batch referring to its ERA source | Receipt stays; its reference alone no longer blocks authorised source cleanup |
| Old logs in explicitly bounded locations | Seven-day minimum age, no related queued/running job and no detected metadata reference |
| Mixed EC packages, FULLSET/FLUXMET/FLUXES observations, BIF, derived forcing, scientific receipts, surface/domain/mesh/PFT data, model outputs/restarts and Git | Never deletion candidates |

There is **no surface-confirmation or seven-day age requirement for other
batches' ERA source data**. Surface creation is not used to infer whether
meteorological sources are expendable; the operator has now specified the
batch retention rule directly. ERA-Interim is outside this ERA5-specific policy.

## Discovery boundaries

The tool inventories ERA-labelled payloads (`.nc`, `.nc4`, `.grib`, `.grb`,
`.grb2`, `.zip`, `.csv`, `.json`) in these locations:

- Site `subset_data/`, site top level, site ERA-labelled directories, and
  `forcing/raw*/`, `forcing/cache/`, `forcing/external_evidence/` or
  `forcing/ERA5*/`.
- Experiment `external_evidence/`, `external_data/`, `cache/`, `cds/` and
  ERA-labelled source directories. Selected/final forcing, DATM, reports and
  science trials are excluded.
- Non-hidden ERA-labelled Home directories.

Payload names or an ancestor within the source store must identify ERA5 /
ERA5-Land. Receipts, manifests, requests, hashes and other scientific bookkeeping
are retained. Original packages that combine EC observations with ERA fields
are retained as a whole. This is a bounded discovery policy, not a filesystem-wide
wildcard deletion; an unknown source location is not silently guessed.

Old-log locations: Home top level; immediate contents of Home directories
starting `ameriflux_`, `AmeriFlux_`, `Batch`, `CLM_FATES_maintenance`; site
top level and `subset_data`; case top level and `logs/`; and
`CTSM_FATES_OUTPUTS/archive/<case>/logs/`. No arbitrary recursive project
deletion, model run/build cleanup, Git packing or empty-directory removal occurs.

## Run

Use the downloaded script from the **latest commit and SHA256 supplied in the
handoff**. Older pinned commands retain their earlier code.

```bash
# Read-only; no files created or removed.
python3 -B "$SCRIPT" audit

# Include every retention reason.
python3 -B "$SCRIPT" audit --details

# Archive, verify and clean under the explicit batch policy.
python3 -B "$SCRIPT" clean --confirm ARCHIVE_VERIFY_CLEAN_ERA_EXCEPT_AMF03_05_AND_OLD_LOGS
myfiles
```

The audit prints
`ERA_POLICY=KEEP_AMF_BATCH03_BATCH05_AND_SHARED_DEPENDENCIES` and reports
`KEEP_AMERIFLUX_BATCH03_OR_BATCH05` for protected ERA files. `--min-days 14`
increases **log** retention; it does not postpone other authorised ERA cleanup.
No job is submitted, cancelled or modified. Full queued/running-job metadata
and model stream/namelist references remain additional protections.

## Backup, verification and restoration

The effective backup destination must be mounted as NFS:
`/research/xclimatetbm/ly3n24/Scratch_Declutter/`.
An unavailable mount stops cleanup. A lock prevents simultaneous maintenance runs.

Every session contains a compressed TAR, `manifest.json` and an append-only
`cleanup_actions.jsonl`. Every archived member and the TAR itself receive
SHA256 verification. Changed files cause a stop or are retained. Dependencies
and queue state are checked again after backup and before deletion; the queue
also refreshes during deletion. Symlinks, symlink ancestors, hard links and
foreign-owned files are not deleted. These private data and receipts never go
to GitHub.

Literal reference scanning is conservative but cannot prove the absence of
every dynamically constructed or future reader. Historical analyses of **other**
batches that need deleted ERA sources must first restore those sources from
the verified archive; retained receipts continue to document original hashes.

`VERIFIED_BACKUP <path>` reports completed verification.
`CLEAN_COMPLETE removed_files=...` reports actual removed originals.
Only `myfiles` establishes the resulting quota usage. Publication and local
tests do not establish that IRIDIS cleanup ran. The operator's earlier snapshot
was 846/2000 GB and 1,011,142/1,228,800 files (217,658 file slots remaining).

Existing v1 and v2 archives can still be verified by the current version:

```bash
python3 -B "$SCRIPT" verify --session /research/xclimatetbm/ly3n24/Scratch_Declutter/<actual_session>
```

Verify first, then restore into a new review directory:

```bash
mkdir -p "$HOME/scratch_cleanup_restore_review"
tar -xzf /research/xclimatetbm/ly3n24/Scratch_Declutter/<actual_session>/files.tar.gz \
  -C "$HOME/scratch_cleanup_restore_review"
```

Member names contain original paths with the leading slash removed. Inspect
the restored files and manifest before copying selected members to their
original locations; do not extract over a live Scratch tree.

## Tests

```bash
python3 -B -m unittest discover -s tests -v
```

Regression tests cover all twenty protected sites, both protected experiment
naming schemes, direct/transitive/shared-link dependencies, other ERA source
eligibility, runtime/queue protection, log retention, archive corruption,
changed sources and exact restoration. Tests use temporary fixtures.

If the bounded audit finds few ERA/log candidates, investigate other inode
consumers separately. Existing
[per-case Git maintenance](https://github.com/lyanyeung/CLM-FATES-single-site-workflows/tree/main/maintenance/cime_git_cleanup)
is a distinct workflow and is not invoked here.
