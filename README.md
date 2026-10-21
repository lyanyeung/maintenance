# IRIDIS6 maintenance — Home archiving and safe cleanup

This repository contains reusable scripts for maintaining the IRIDIS6 Home directory. **GitHub stores maintenance code and documentation only. Research data, model outputs, logs, archive files, and verification manifests must be kept on the Southampton Research Filestore and must never be committed to this public repository.**

## Implemented features

- **`audit` (read-only):** Classifies non-hidden files at the top level of Home and lists old logs, Slurm previews, historical archives, and project directories that have not been cleaned up.
- **`backup` (backup):** Writes directly to `/research/xclimatetbm/ly3n24/Home_Declutter/`. Creates a UTC-stamped `home_snapshot_YYYYMMDDTHHMMSSZ/` directory containing `home_selected.tar.gz`, `manifest.json`, and `SHA256SUMS`. Verifies every archived file with SHA-256 and compares it against its source. Stops without cleaning anything if a source file has changed.
- **`verify` (read-only):** Rechecks the full archive's SHA-256 checksums and each TAR member.
- **`clean` (conditional cleanup):** With explicit authorization, considers only regular files at the top level of Home that have been fully archived, verified file by file, and not modified for at least **48 hours**: `*.log`, `*.out`, `*.err`, `*.bak`, `*.zip`, `*_preview_run.txt`, and `*.before_*`. Rechecks hashes and modification times and looks for explicit references in Slurm jobs. Records each action in the archive session's `cleanup_actions.tsv`. By default, never deletes directories, Python/Shell source files, CSV files, or Git projects.
- **Protected active directories:** Examples include `CARDAMOM`, `CLM-FATES-single-site-workflows`, `FLUXNET_MultiSite_11`, `CLM_FATES_13_checkpoint`, `TCCAS-workflows`, and this `maintenance` repository. Hidden directories such as `.ssh`, `.conda`, and `.git` are skipped entirely.
- **Mount verification:** Confirms that the Research Filestore is mounted as NFS before writing any archive, preventing accidental writes to an unmounted local path. Compatible with IRIDIS6 Python 3.9.

## Running on IRIDIS6

```bash
cd "$HOME"
git clone https://github.com/lyanyeung/maintenance.git maintenance
cd "$HOME/maintenance"

# 1. Inspect and classify files without making changes
python3 scripts/archive_home.py audit

# 2. Create and verify a complete backup without deleting files
python3 scripts/archive_home.py backup
```

A successful backup prints `VERIFIED_BACKUP <SESSION>` and the complete `clean` command for the next step. **Proceed only after seeing `VERIFIED_BACKUP`.** First review whether other projects manually reference any files selected for cleanup:

```bash
# 3. Replace <SESSION> with the actual path printed above; verify separately
python3 scripts/archive_home.py verify --session "<SESSION>"

# 4. Perform conditional cleanup only after reviewing the selection
python3 scripts/archive_home.py clean --session "<SESSION>" \
  --confirm DELETE_OLD_VERIFIED_FILES

# 5. Inspect the cleanup record and quota
cat "<SESSION>/cleanup_actions.tsv"
myfiles
```

If the repository is already cloned, run `cd "$HOME/maintenance" && git pull --ff-only` instead of cloning it again.

**One-command backup and verification, with no deletion:**

```bash
bash scripts/run_home_maintenance.sh
```

Cleanup is a separate step requiring explicit authorization. Deletion without verification is not supported.

## Restoring an archive

First extract the archive into a **new review directory**; do not overwrite the existing Home directory:

```bash
SESSION="/research/xclimatetbm/ly3n24/Home_Declutter/home_snapshot_<ACTUAL_TIMESTAMP>"
mkdir -p "$HOME/home_restore_review"
tar -xzf "$SESSION/home_selected.tar.gz" -C "$HOME/home_restore_review"
# Extracted files: $HOME/home_restore_review/home/
```

Note: If a review directory such as `home_restore_review` is created inside Home, it can become a candidate for the next `backup` run. Review and remove or relocate it manually when finished to avoid repeatedly nesting archives.

## Archive destinations and cleanup scope

| Path | Purpose | Automatically deleted? |
|---|---|---|
| Old top-level logs and similar files in `/iridisfs/home/ly3n24` | Primary cleanup target | Only by `clean`, after all checks pass |
| `/research/xclimatetbm/ly3n24/Home_Declutter/` | Research backup destination | No |
| `/scratch/ly3n24/CTSM_FATES_OUTPUTS/` | Active CLM-FATES model outputs | **Never touched** |
| `/iridisfs/scratch/ly3n24/CLM_FATES/cases/` | Active CIME cases | **Never touched** |
| GitHub `maintenance` | Maintenance-tool source code | No |

**Important distinction: The Home maintenance workflow described above does not free Scratch inodes.** At the time this workflow was documented, Scratch had approximately 560,000 files against a quota of 614,400. Reclaiming Scratch quota requires a separate, site-specific process that preserves checkpoints and verifies archives. Never mass-delete directories belonging to running CLM-FATES simulations.

## Safety boundaries

- Having a script with the same name on GitHub does **not** mean its IRIDIS copy is safe to delete. Preserve any local `.py` or `.sh` file that has not been compared byte for byte with the GitHub version.
- An incomplete `backup`, `ls`, or `df -i` is not proof of a safe transfer. Rely on `VERIFIED_BACKUP`, `VERIFIED_SESSION`, and verification receipts.
- The absence of a file path from the Slurm queue does not rule out references from cron, interactive shells, or external workflows. Check these dependencies before automated cleanup.
- The Home cleanup workflow does not run `scancel`, `rm -rf`, or any Scratch/CIME deletion logic. It unlinks only individually selected regular files in Home.
- This repository **does not contain** actual user archives or records proving cleanup was executed. Publishing a GitHub commit does not imply that files on IRIDIS were moved or deleted.

## FLUXNET raw-data audit and DD/MM/YY cleanup (separate tool)

[Full instructions](docs/FLUXNET_RAW_CLEANUP.md) · [Python script](scripts/fluxnet_raw_cleanup.py)

There is no need to clone the repository on IRIDIS6, save a script, or generate a report. Run the read-only audit directly from GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/lyanyeung/maintenance/main/scripts/fluxnet_raw_cleanup.py | python3 -B - --details summary
```

The audit counts HH / BIF / DD / MM / YY CSV files and the original ZIP files in each raw release directory. By default, it prints results to the terminal only; it creates no files and deletes no data. Deletion requires explicit `--apply --confirm DELETE_DD_MM_YY` and applies only to DD/MM/YY files with a corresponding HH file of the same name stem. **ZIP files are never deleted.** DATM files, CIME cases, restarts, and model history NetCDF files are untouched. Before deletion, confirm any requirements to retain official aggregated products and ensure an independent backup exists.
