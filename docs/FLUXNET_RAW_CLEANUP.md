# FLUXNET raw file cleanup on IRIDIS6

This tool is intentionally separate from the existing Home maintenance. It targets **only the original FLUXNET source files** under `/iridisfs/scratch/ly3n24/CLM_FATES/sites/MY_EC_SITE/<SITE>/forcing/raw*/`.

## Run without cloning or creating files

Use the system Python 3 and stream the source directly from GitHub into stdin. `-B` disables `__pycache__` generation; neither the script nor its default reports are saved.

```bash
URL="https://raw.githubusercontent.com/lyanyeung/maintenance/main/scripts/fluxnet_raw_cleanup.py"
curl -fsSL "$URL" | python3 -B - --details summary
```

Inspect every proposed DD/MM/YY CSV before deleting:

```bash
curl -fsSL "$URL" | python3 -B - --details candidates
```

List remaining source ZIPs (inventory only):

```bash
curl -fsSL "$URL" | python3 -B - --details zips
```

Optionally inspect unmatched aggregate CSVs:

```bash
curl -fsSL "$URL" | python3 -B - --details review
```

Check quota before and after with `myfiles`.

## Explicit deletion (only after reviewing candidates and confirming backups)

```bash
curl -fsSL "$URL" | python3 -B - --apply --confirm DELETE_DD_MM_YY
myfiles
```

This does **not** remove ZIPs. Only DD/MM/YY CSVs with the **exact same filename except `_HH_` in place of `_DD_`, `_MM_`, or `_YY_`**, in the **same raw directory**, and nonempty HH counterparts are candidates. The script checks file identities again before each deletion. The presence of a matching HH is a file-naming safeguard, **not** scientific validation of aggregation or proof of external backups.

**Retain** `FLUXMET_HH`, `FLUXES_HH`, `FULLSET_HH`, `ERA5_HH`, `ERAI_HH`, BIF, all other HH CSV, processed forcing, DATM NetCDF, and the active CIME/restart/historical output. Special cases include 11-site multisource HH and IT-Col FLUXNET2015 FULLSET_HH/ERAI_HH. Preserve official DD/MM/YY products externally if their QC and aggregation methods are needed, and review source licences.

Source ZIPs are **only counted**; the older 6-/8-/13-site extractors remove temporary *nested* ZIPs but do not remove original outer archives. Do not delete these until the complete extraction and off-Scratch backup have been independently verified.

No output files are created by default. For optional TSVs, provide `--report-dir /research/xclimatetbm/ly3n24/FLUXNET_raw_audit`; that will create files on Research Filestore rather than Scratch. Avoid piping through `tee` to a Scratch path. The scan may take some time and uses CPU / filesystem metadata requests but normally needs no Slurm job and creates no permanent inode.

This procedure does not archive files, prove backups exist, or identify all possible downstream readers. **Always audit first**. It will not touch the currently running model cases, but review any old analysis code that uses official DD/MM/YY products before deleting them.
