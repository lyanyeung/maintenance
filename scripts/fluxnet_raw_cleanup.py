#!/usr/bin/env python3
"""Audit FLUXNET raw releases on IRIDIS6; optionally remove redundant DD/MM/YY CSV.

Default: read-only, stdout only, no files created.  Does not delete ZIP,
HH observations, BIF, ERA, forcing NetCDF, model output or restart files.
Compatible with Python 3.9+; no external Python packages required.
"""

import argparse
import csv
import os
import re
import stat
from collections import defaultdict
from pathlib import Path

DEFAULT_ROOT = Path('/iridisfs/scratch/ly3n24/CLM_FATES/sites/MY_EC_SITE')
AGGREGATE = re.compile(r'_(DD|MM|YY)_', re.IGNORECASE)
FIELDS = ('raw_csv', 'HH', 'BIF', 'DD', 'MM', 'YY', 'eligible', 'review', 'ZIP')


def files_under(directory):
    """Yield ordinary files only; never descend into symlink directories."""
    if not directory.is_dir() or directory.is_symlink():
        return

    def onerror(error):
        raise error

    for parent, subdirs, names in os.walk(str(directory), followlinks=False, onerror=onerror):
        subdirs[:] = [name for name in subdirs if not (Path(parent) / name).is_symlink()]
        for name in names:
            path = Path(parent) / name
            if not path.is_symlink() and path.is_file():
                yield path


def fmt_gib(n):
    return '{:.3f} GiB'.format(n / (1024 ** 3))


def signature(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def audit(root):
    counters = defaultdict(lambda: defaultdict(int))
    candidates, needs_review, packages = [], [], []

    for site_root in sorted(root.iterdir()):
        if not site_root.is_dir() or site_root.is_symlink() or site_root.name.startswith('_'):
            continue
        forcing = site_root / 'forcing'
        if not forcing.is_dir() or forcing.is_symlink():
            continue
        site = site_root.name
        # Inspect forcing tree for ZIPs; consider only CSVs inside raw* directories.
        for path in files_under(forcing):
            name = path.name
            if path.suffix.lower() == '.zip':
                size = path.stat().st_size
                packages.append((site, str(path), size))
                counters[site]['ZIP'] += 1
                counters[site]['ZIP_bytes'] += size
                continue
            if path.suffix.lower() != '.csv':
                continue
            relative_parts = path.relative_to(forcing).parts
            if len(relative_parts) < 2 or not relative_parts[0].lower().startswith('raw'):
                continue
            old = signature(path)
            if old is None:
                continue
            size = old[2]
            counters[site]['raw_csv'] += 1
            counters[site]['raw_bytes'] += size
            upper = name.upper()
            if '_HH_' in upper:
                counters[site]['HH'] += 1
            if 'BIF' in upper:
                counters[site]['BIF'] += 1
            match = AGGREGATE.search(name)
            if match is None:
                continue
            kind = match.group(1).upper()
            counters[site][kind] += 1
            # Require the exact same release / stem / date range, differing only in frequency.
            twin = path.with_name(name[:match.start()] + '_HH_' + name[match.end():])
            row = (site, kind, str(path), size, str(twin))
            twin_sig = signature(twin) if twin.exists() and not twin.is_symlink() else None
            if twin_sig is not None and twin_sig[2] > 0:
                candidates.append((path, old, twin, twin_sig, row))
                counters[site]['eligible'] += 1
                counters[site]['eligible_bytes'] += size
            else:
                needs_review.append(row)
                counters[site]['review'] += 1

    incoming = root / '_incoming_zips'
    for path in files_under(incoming):
        if path.suffix.lower() == '.zip':
            size = path.stat().st_size
            packages.append(('_incoming_zips', str(path), size))
            counters['_incoming_zips']['ZIP'] += 1
            counters['_incoming_zips']['ZIP_bytes'] += size
    return counters, candidates, needs_review, packages


def write_tsv(path, header, rows):
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f, delimiter='\t')
        writer.writerow(header)
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(os.getenv('FLUXNET_SITES_ROOT', str(DEFAULT_ROOT))))
    parser.add_argument('--details', choices=('summary', 'candidates', 'review', 'zips', 'all'),
                        default='summary', help='Optional detailed lists on stdout')
    parser.add_argument('--report-dir', type=Path,
                        help='Optional TSV output directory; no reports are written by default')
    parser.add_argument('--apply', action='store_true', help='Unlink only exact-HH-matched DD/MM/YY CSV')
    parser.add_argument('--confirm', default='', help='Required DELETE_DD_MM_YY confirmation for --apply')
    args = parser.parse_args()

    if args.root.is_symlink():
        parser.error('Refusing a symlink root')
    root = args.root.resolve()
    if not root.is_dir() or root.name != 'MY_EC_SITE':
        parser.error('Expected existing MY_EC_SITE directory; got {}'.format(root))
    if args.apply and args.confirm != 'DELETE_DD_MM_YY':
        parser.error('--apply requires --confirm DELETE_DD_MM_YY')

    counters, candidates, review, packages = audit(root)
    print('ROOT:', root)
    print('MODE:', 'APPLY DD/MM/YY ONLY' if args.apply else 'READ-ONLY / NO FILE WRITES')
    print('\n{:<14} {:>7} {:>5} {:>5} {:>5} {:>5} {:>5} {:>8} {:>7} {:>5}'.format('SITE', *FIELDS))
    print('-' * 81)
    for site, data in sorted(counters.items()):
        print('{:<14} {:>7} {:>5} {:>5} {:>5} {:>5} {:>5} {:>8} {:>7} {:>5}'.format(
            site[:14], *(data[key] for key in FIELDS)))
    eligible_bytes = sum(x[4][3] for x in candidates)
    zip_bytes = sum(x[2] for x in packages)
    print('\nCANDIDATE DD/MM/YY: {} files, {}'.format(len(candidates), fmt_gib(eligible_bytes)))
    print('REVIEW (no exact HH): {} files'.format(len(review)))
    print('ZIP INVENTORY ONLY: {} files, {} (NEVER deleted)'.format(len(packages), fmt_gib(zip_bytes)))

    if args.details in ('candidates', 'all'):
        print('\nCANDIDATES\nsite\tfreq\tpath\tbytes\texact_HH')
        for item in candidates:
            print('\t'.join(map(str, item[4])))
    if args.details in ('review', 'all'):
        print('\nREVIEW\nsite\tfreq\tpath\tbytes\texpected_HH')
        for row in review:
            print('\t'.join(map(str, row)))
    if args.details in ('zips', 'all'):
        print('\nZIPS\nsite\tpath\tbytes')
        for row in packages:
            print('\t'.join(map(str, row)))

    if args.report_dir:
        target = args.report_dir.expanduser().resolve()
        if target == root or root in target.parents:
            parser.error('Report directory must be outside MY_EC_SITE')
        target.mkdir(parents=True, exist_ok=True)
        write_tsv(target / 'eligible_DD_MM_YY.tsv', ('site','frequency','path','bytes','HH_counterpart'),
                  (item[4] for item in candidates))
        write_tsv(target / 'review_no_exact_HH.tsv', ('site','frequency','path','bytes','expected_HH'), review)
        write_tsv(target / 'original_ZIP.tsv', ('site','path','bytes'), packages)
        print('REPORTS WRITTEN:', target)
    else:
        print('REPORTS: none (stdout only)')

    if not args.apply:
        print('DRY RUN: NOTHING DELETED')
        return 0

    removed = 0
    freed = 0
    for path, old_sig, twin, twin_sig, row in candidates:
        # Revalidate at deletion time: refuse altered sources or HH twins.
        if path.is_symlink() or twin.is_symlink() or not path.is_file() or not twin.is_file():
            print('SKIP: missing or symlink:', path)
            continue
        if signature(path) != old_sig or signature(twin) != twin_sig:
            print('SKIP: file changed:', path)
            continue
        try:
            path.unlink()
        except OSError as error:
            print('ERROR: {}: {}'.format(path, error))
            continue
        removed += 1
        freed += old_sig[2]
    print('DELETED: {} DD/MM/YY files, {} nominal size'.format(removed, fmt_gib(freed)))
    print('ZIP / HH / BIF / ERA / DATM / CIME / restarts: untouched')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
