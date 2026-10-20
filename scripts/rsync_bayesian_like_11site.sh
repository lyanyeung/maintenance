#!/usr/bin/env bash
# Bayesian transfer copied from the proven FLUXNET_MultiSite_11 RFS pattern.
# Login node ONLY. Modes: copy, verify, status. NEVER deletes source files.
set -Eeuo pipefail
umask 077

NAME="Bayesian_Meta_Cuptake_RawSE_SixScenarios_Iridis6"
SRCROOT="/iridisfs/scratch/ly3n24"
SRC="$SRCROOT/$NAME"
OLD="$SRCROOT/$NAME.tar.gz"
RFS="/research/xclimatetbm/ly3n24"
DESTROOT="$RFS/Bayesian_Meta_Cuptake_Archive"
DST="$DESTROOT/$NAME"
OLD_DST="$DESTROOT/prior_versions/$NAME.tar.gz"
DIFFLOG="$DESTROOT/rsync_verification_differences.txt"
RECEIPT="$DESTROOT/rsync_verification_receipt.txt"

check_mount_and_sources() {
  test -d "$SRC" && test ! -L "$SRC"
  test -f "$OLD" && test ! -L "$OLD"
  test -d "$RFS" && test -w "$RFS"
  FS="$(stat -f -c %T "$RFS")"
  case "$FS" in nfs|nfs4) ;; *) echo "STOP: Research Filestore mount not NFS ($FS)" >&2; exit 1;; esac
}
verify_all() {
  check_mount_and_sources
  test -d "$DST" && test -f "$OLD_DST"
  echo "VERIFY: source and destination, read-only rsync checksum scan"
  # The -n dry-run flag makes --delete a read-only DIFFERENCE detector.
  # Differences include missing, extra, changed files, links and metadata.
  rsync -acni --delete --no-owner --no-group --no-perms \
    --out-format='%i %n' -- "$SRC/" "$DST/" > "$DIFFLOG"
  if test -s "$DIFFLOG"; then
    echo "STOP: Differences detected. Examples:" >&2
    head -n 25 "$DIFFLOG" >&2
    exit 1
  fi
  cmp -- "$OLD" "$OLD_DST"
  echo "VERIFIED_COPY"
}
run_copy() {
  check_mount_and_sources
  mkdir -p "$DST" "$DESTROOT/prior_versions"
  echo "COPY: $SRC -> $DST"
  du -sh "$SRC" "$OLD"
  # Like the 11-site process: rsync can be re-run after a connection interruption.
  # --no-owner/group/perms lets Research Filestore retain its managed permissions.
  rsync -avh --partial --info=progress2 \
    --no-owner --no-group --no-perms \
    -- "$SRC/" "$DST/"
  echo "COPY original small historical tar.gz (may contain different contents)"
  rsync -avh --partial --no-owner --no-group --no-perms \
    -- "$OLD" "$OLD_DST"
  verify_all
  {
    date -u +"verified_utc=%Y-%m-%dT%H:%M:%SZ"
    printf 'source=%s\n' "$SRC"
    printf 'destination=%s\n' "$DST"
    printf 'old_archive_backup=%s\n' "$OLD_DST"
    printf 'validation=rsync checksum dry-run plus original tar byte compare\n'
    printf 'scratch_deleted=no\n'
  } > "$RECEIPT"
  echo "BACKUP_COMPLETE_NO_DELETION"
  ls -lh "$OLD_DST" "$RECEIPT"
  du -sh "$DST"
}
mode="$1"
case "$mode" in
  copy|verify)
    exec 9>"$HOME/.bayesian_rfs_rsync.lock"
    flock -n 9 || { echo "Another copy/verify process holds the lock"; exit 1; }
    case "$mode" in copy) run_copy ;; verify) verify_all ;; esac
    ;;
  status)
    echo "=== Research destination ==="
    if test -d "$DESTROOT"; then
      du -sh "$DST" 2>/dev/null || true
      ls -lh "$OLD_DST" "$RECEIPT" 2>/dev/null || true
    fi
    echo "=== Background copy processes ==="
    pgrep -af 'rsync.*Bayesian_Meta_Cuptake' || true
    echo "=== Recent copy log ==="
    tail -n 12 "$HOME/Bayesian_rsync_transfer.log" 2>/dev/null || true
    ;;
  *) echo "Usage: bash $0 copy|verify|status" >&2; exit 2;;
esac
