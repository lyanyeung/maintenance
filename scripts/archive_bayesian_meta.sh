#!/usr/bin/env bash
# Fast Bayesian Meta Cuptake archive: submit Slurm job, verify, then explicit cleanup.
# Tested design for IRIDIS6. Data stays in Research Filestore; never commit data to GitHub.
set -Eeuo pipefail

NAME="Bayesian_Meta_Cuptake_RawSE_SixScenarios_Iridis6"
BASE="/iridisfs/scratch/ly3n24"
SOURCE="$BASE/$NAME"
OLD="$BASE/$NAME.tar.gz"
DEST="/research/xclimatetbm/ly3n24/Bayesian_Meta_Cuptake_Archive"
FINAL="$DEST/$NAME.tar.gz"
RECEIPT="$DEST/$NAME.verified.txt"
SELF="$(readlink -f -- "$0")"

fail() { echo "STOP: $*" >&2; exit 2; }
source_ok() {
  [[ -d "$SOURCE" && ! -L "$SOURCE" ]] || fail "Directory missing: $SOURCE"
  [[ -f "$OLD" && ! -L "$OLD" ]] || fail "Old archive missing: $OLD"
  [[ "$(realpath -e -- "$SOURCE")" == "$SOURCE" ]] || fail "Source path mismatch"
}
filestore_ok() {
  [[ -d /research/xclimatetbm/ly3n24 ]] || fail "Research Filestore inaccessible"
  case "$(stat -f -c %T /research/xclimatetbm/ly3n24)" in
    nfs|nfs4) ;;
    *) fail "Research Filestore NFS mount not confirmed" ;;
  esac
}
snapshot() {
  find "$SOURCE" -printf '%P\t%y\t%s\t%T@\t%l\0' |
    LC_ALL=C sort -z | sha256sum | awk '{print $1}'
}
jobs_safe() {
  local queue jid jobname details
  queue="$(squeue -h -u "$USER" -o '%i|%j')" || fail "Cannot query Slurm queue"
  while IFS='|' read -r jid jobname; do
    [[ -n "$jid" ]] || continue
    [[ "$jobname" != *BayesArch* && "$jobname" != *Bayesian* &&
       "$jobname" != *Cuptake* ]] || fail "Active Bayesian job: $jid"
    details="$(scontrol show job -o "$jid")" || fail "Cannot inspect job $jid"
    [[ "$details" != *"$SOURCE"* && "$details" != *"$OLD"* ]] ||
      fail "Job $jid references original source"
  done <<< "$queue"
}
submit_job() {
  source_ok; filestore_ok
  [[ ! -e "$FINAL" && ! -e "$RECEIPT" ]] ||
    fail "Destination/receipt already exists; review it before resubmitting"
  [[ -z "$(squeue -h -u "$USER" -n BayesArch)" ]] ||
    fail "A BayesArch job is already in queue"
  echo "Source:"
  du -sh "$SOURCE" "$OLD"
  echo "Submitting independent 4-CPU Slurm task..."
  sbatch --parsable --job-name=BayesArch --partition=batch \
    --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=16G --time=12:00:00 \
    --output="$HOME/BayesArch_%j.out" --error="$HOME/BayesArch_%j.err" \
    "$SELF" worker
  echo "SSH may disconnect without cancelling this Slurm job."
  echo "To view progress: bash $SELF status"
}
worker() {
  [[ -n "$(printenv SLURM_JOB_ID || true)" ]] ||
    fail "worker only runs inside Slurm"
  source_ok; filestore_ok
  mkdir -p "$DEST/prior_versions"
  [[ ! -e "$FINAL" && ! -e "$RECEIPT" ]] ||
    fail "Final archive already exists; refusing overwrite"
  PARTIAL="$DEST/.$NAME.$SLURM_JOB_ID.partial.tar.gz"
  OLDTMP="$DEST/prior_versions/.old_$SLURM_JOB_ID.partial.tar.gz"
  [[ ! -e "$PARTIAL" && ! -e "$OLDTMP" ]] ||
    fail "An unfinished file already exists for this job"
  echo "[$(date -Is)] BEGIN compression"
  BEFORE="$(snapshot)"
  if command -v pigz >/dev/null 2>&1; then
    echo "Using pigz -1 (parallel, 4 cores)"
    tar --sparse -C "$BASE" -cf - "$NAME" |
      pigz -1 -p "$SLURM_CPUS_PER_TASK" > "$PARTIAL"
  else
    echo "pigz unavailable; using gzip -1 (single core)"
    tar --sparse -C "$BASE" -cf - "$NAME" | gzip -1 > "$PARTIAL"
  fi
  echo "[$(date -Is)] Compression finished. Checking gzip and TAR."
  tar -tzf "$PARTIAL" >/dev/null
  AFTER="$(snapshot)"
  [[ "$BEFORE" == "$AFTER" ]] ||
    fail "Source changed during compression; original remains intact"
  [[ -s "$PARTIAL" ]] || fail "Archive unexpectedly empty"
  HASH="$(sha256sum "$PARTIAL" | awk '{print $1}')"
  OLDHASH="$(sha256sum "$OLD" | awk '{print $1}')"
  STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  PRIOR="$DEST/prior_versions/$NAME.old.$STAMP.$SLURM_JOB_ID.tar.gz"
  [[ ! -e "$PRIOR" ]] || fail "Prior archive already exists"
  cp -p -- "$OLD" "$OLDTMP"
  cmp -s -- "$OLD" "$OLDTMP" || fail "Old archive copy verification failed"
  mv -- "$OLDTMP" "$PRIOR"
  mv -- "$PARTIAL" "$FINAL"
  {
    printf 'source_fingerprint=%s\n' "$BEFORE"
    printf 'archive_sha256=%s\n' "$HASH"
    printf 'old_sha256=%s\n' "$OLDHASH"
    printf 'prior_archive=%s\n' "$PRIOR"
    printf 'verified_utc=%s\n' "$STAMP"
    printf 'slurm_job=%s\n' "$SLURM_JOB_ID"
  } > "$DEST/.$NAME.$SLURM_JOB_ID.receipt.partial"
  mv -- "$DEST/.$NAME.$SLURM_JOB_ID.receipt.partial" "$RECEIPT"
  echo "[$(date -Is)] VERIFIED_ARCHIVE"
  ls -lh "$FINAL" "$PRIOR"
  echo "Original files have NOT been removed. Cleanup is a separate opt-in step."
}
status() {
  echo "=== Slurm ==="
  squeue -u "$USER" -n BayesArch -o '%.12i %.9T %.12M %.8C %.4D %.30R' || true
  echo "=== Logs ==="
  ls -lt "$HOME"/BayesArch_*.out "$HOME"/BayesArch_*.err 2>/dev/null | head -8 || true
  echo "=== Transfer progress ==="
  ls -lh "$DEST"/."$NAME".*.partial.tar.gz "$FINAL" "$RECEIPT" 2>/dev/null || true
  echo "=== Originals ==="
  du -sh "$SOURCE" "$OLD" 2>/dev/null || true
}
cleanup() {
  source_ok; filestore_ok
  [[ -f "$RECEIPT" && ! -L "$RECEIPT" ]] ||
    fail "Verified receipt absent"
  EXPECT_SOURCE="$(sed -n 's/^source_fingerprint=//p' "$RECEIPT")"
  EXPECT_HASH="$(sed -n 's/^archive_sha256=//p' "$RECEIPT")"
  EXPECT_OLD="$(sed -n 's/^old_sha256=//p' "$RECEIPT")"
  PRIOR="$(sed -n 's/^prior_archive=//p' "$RECEIPT")"
  [[ "$EXPECT_SOURCE" =~ ^[0-9a-f]{64}$ &&
     "$EXPECT_HASH" =~ ^[0-9a-f]{64}$ &&
     "$EXPECT_OLD" =~ ^[0-9a-f]{64}$ ]] || fail "Invalid receipt"
  [[ "$PRIOR" == "$DEST/prior_versions/"* &&
     -f "$PRIOR" && ! -L "$PRIOR" ]] || fail "Prior archive missing"
  [[ -f "$FINAL" && ! -L "$FINAL" ]] || fail "New archive missing"
  jobs_safe
  echo "Verifying Research Filestore archive hashes..."
  [[ "$(sha256sum "$FINAL" | awk '{print $1}')" == "$EXPECT_HASH" ]] ||
    fail "Final archive hash differs"
  [[ "$(sha256sum "$PRIOR" | awk '{print $1}')" == "$EXPECT_OLD" ]] ||
    fail "Prior archive hash differs"
  [[ "$(sha256sum "$OLD" | awk '{print $1}')" == "$EXPECT_OLD" ]] ||
    fail "Old original changed"
  [[ "$(snapshot)" == "$EXPECT_SOURCE" ]] ||
    fail "Source changed since archive creation"
  # Preserve originals if any link references an external path.
  while IFS= read -r -d '' link; do
    target="$(realpath -m -- "$link")"
    [[ "$target" == "$SOURCE" || "$target" == "$SOURCE/"* ]] ||
      fail "External symlink: $link -> $target"
  done < <(find "$SOURCE" -type l -print0)
  echo "Verified new: $FINAL"
  echo "Verified old: $PRIOR"
  echo "Originals to delete: $SOURCE and $OLD"
  read -r -p "Type DELETE to remove both originals: " ANSWER
  [[ "$ANSWER" == DELETE ]] || { echo "Originals retained"; exit 0; }
  jobs_safe
  [[ "$(snapshot)" == "$EXPECT_SOURCE" ]] ||
    fail "Source changed immediately before deletion"
  rm -rf --one-file-system -- "$SOURCE"
  rm -f -- "$OLD"
  echo "MOVED_AND_REMOVED"
  command -v myfiles >/dev/null 2>&1 && myfiles || true
}
case "$1" in
  submit) submit_job ;;
  worker) worker ;;
  status) status ;;
  cleanup) cleanup ;;
  *) echo "Usage: bash $SELF {submit|status|cleanup}" >&2; exit 2 ;;
esac
