#!/usr/bin/env python3
"""IRIDIS6 unused surface ERA cache / old log cleanup, Python 3.9+ stdlib.

audit: read-only. clean: archive on NFS, verify every member, then unlink
unchanged eligible files. Original forcing, external evidence, scientific
receipts, model history/restarts, source trees and Git are never candidates.
"""
import argparse
import collections
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import time
import uuid

VERSION = '20261027-v1'
TOKEN = 'ARCHIVE_VERIFY_CLEAN_OLD_CACHE_LOGS'
SCRATCH = Path('/iridisfs/scratch/ly3n24')
HOME = Path('/iridisfs/home/ly3n24')
BACKUP = Path('/research/xclimatetbm/ly3n24/Scratch_Declutter')
SITE_RE = re.compile(r'^[A-Z]{2}-[A-Za-z0-9]+$')
ERA_RE = re.compile(r'era[-_]?5(?:[-_]?land)?|era[-_]?interim|\berai\b', re.I)
STATIC_RE = re.compile(r'surfdata|domain|mesh|landuse|pft|snow|soil', re.I)
LOG_RE = re.compile(r'(?:\.log(?:\.[A-Za-z0-9_-]+)*|\.(?:out|err))(?:\.gz)?$', re.I)
META_SUFFIXES = {'.json', '.xml', '.nml', '.streams', '.cfg', '.yaml', '.yml', '.tsv',
                 '.txt', '.sha256', '.py', '.sh'}
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', 'history', 'hist', 'rest', 'restart',
             'raw', 'packages', 'inputdata', 'bld', 'Buildconf'}


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def command(argv):
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=90)
    require(result.returncode == 0, '{}: {}'.format(argv[0], result.stderr.strip()))
    return result.stdout


def ordinary(path):
    """No symlinks at any component; also reject hard links and foreign owners."""
    path = Path(path)
    try:
        if any(p.is_symlink() for p in (path, *path.parents)):
            return None
        s = path.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_uid != os.getuid():
            return None
        return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]
    except FileNotFoundError:
        return None


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def walk(root, prune=()):
    if not root.is_dir() or root.is_symlink():
        return
    def error(exc):
        raise exc
    for folder, dirs, files in os.walk(str(root), followlinks=False, onerror=error):
        dirs[:] = [d for d in dirs if d not in prune and not d.startswith('.')
                   and not (Path(folder) / d).is_symlink()]
        for name in sorted(files):
            path = Path(folder) / name
            if not name.startswith('.') and not path.is_symlink():
                yield path


def topfiles(root):
    if root.is_dir() and not root.is_symlink():
        yield from (p for p in sorted(root.iterdir()) if p.is_file() and not p.is_symlink())


def textfile(path):
    if path.suffix.lower() in META_SUFFIXES:
        return True
    name = path.name.lower()
    return (name in {'lnd_in', 'datm_in', 'user_nl_clm', 'user_nl_datm', 'casestatus', 'sha256sums'}
            or 'streams' in name or (path.suffix.lower() == '.csv' and
                any(tag in name for tag in ('manifest', 'receipt', 'provenance', 'sha256'))))


def queue():
    """Query full job metadata, including pending stdout/stderr paths."""
    ids = command(['squeue', '-r', '-h', '-u', getpass.getuser(), '-o', '%i']).split()
    jobs = []
    for jobid in ids:
        require(re.fullmatch(r'[0-9_+.[\]%-]+', jobid) is not None, 'Unexpected Slurm ID')
        try:
            raw = command(['scontrol', 'show', 'job', '-o', jobid])
        except RuntimeError:
            # A job can leave the queue between squeue and scontrol.
            current = command(['squeue', '-r', '-h', '-u', getpass.getuser(), '-o', '%i']).split()
            if jobid not in current:
                continue
            raise
        fields = dict(re.findall(r'(?:^|\s)([A-Za-z][A-Za-z0-9_]*)=(.*?)(?=\s+[A-Za-z][A-Za-z0-9_]*=|$)', raw.strip()))
        require(fields.get('JobId') and fields.get('JobName') and fields.get('WorkDir', '').startswith('/'),
                'Cannot identify Slurm job and working directory: ' + jobid)
        jobs.append(fields)
    return jobs


def active_paths(jobs, scratch):
    protected, cases, sites = set(), set(), set()
    for job in jobs:
        for key in ('WorkDir', 'StdOut', 'StdErr', 'Command'):
            value = job.get(key, '')
            if value.startswith('/'):
                protected.add(str(Path(value).resolve()))
        name = job['JobName']
        if name.startswith(('run.', 'st_archive.')):
            case = name.split('.', 1)[1]
            cases.add(case)
            match = re.match(r'^([A-Z]{2}-[A-Za-z0-9]+)(?:_|$)', case)
            if match:
                sites.add(match.group(1))
        # Full WorkDir is also used: no dependence on truncated display names.
        work = Path(job['WorkDir']).resolve()
        for parent in (work, *work.parents):
            if parent.parent in {scratch / 'CLM_FATES' / 'cases',
                                 scratch / 'CTSM_FATES_OUTPUTS',
                                 scratch / 'CTSM_FATES_OUTPUTS' / 'archive'} and parent.name != 'archive':
                cases.add(parent.name)
                match = re.match(r'^([A-Z]{2}-[A-Za-z0-9]+)(?:_|$)', parent.name)
                if match:
                    sites.add(match.group(1))
    for case in cases:
        for base in (scratch / 'CLM_FATES' / 'cases', scratch / 'CTSM_FATES_OUTPUTS',
                     scratch / 'CTSM_FATES_OUTPUTS' / 'archive'):
            protected.add(str(base / case))
    return protected, sites


def active(row, protected, sites):
    if row.get('site') in sites:
        return True
    path = Path(row['path'])
    return any(path == Path(p) or Path(p) in path.parents for p in protected)


def metadata_roots(scratch, home):
    # Do not walk million-file model output or Git trees.
    roots = [scratch / 'CLM_FATES' / 'cases', scratch / 'CLM_FATES' / 'experiments',
             scratch / 'CLM_FATES' / 'sites' / 'MY_EC_SITE']
    roots.extend(p for p in home.iterdir() if p.is_dir() and not p.is_symlink()
                 and p.name.startswith(('ameriflux_', 'AmeriFlux_', 'Batch', 'CLM_FATES_maintenance')))
    roots.extend(home / name for name in ('CLM-FATES-single-site-workflows',
                                          'Non-Competition-CLM-experiment', 'maintenance'))
    return roots


def metadata(scratch, home):
    seen = set()
    for root in metadata_roots(scratch, home):
        for path in walk(root, SKIP_DIRS):
            if path not in seen and textfile(path) and not LOG_RE.search(path.name):
                seen.add(path)
                yield path
    for path in topfiles(home):
        if textfile(path) and not LOG_RE.search(path.name):
            yield path


def surface(path):
    if ordinary(path) is None or path.stat().st_size < 8 or STATIC_RE.search(path.name) is None:
        return False
    with path.open('rb') as handle:
        return handle.read(8).startswith((b'CDF', b'\x89HDF\r\n\x1a\n'))


def discover(scratch, home):
    rows = {}
    def add(path, kind, site='', case=''):
        sig = ordinary(path)
        if sig is not None:
            rows[str(path)] = dict(path=str(path), kind=kind, site=site, case=case,
                                   signature=sig, bytes=sig[2])
    sitesroot = scratch / 'CLM_FATES' / 'sites' / 'MY_EC_SITE'
    if sitesroot.is_dir():
        for sitepath in sorted(sitesroot.iterdir()):
            if sitepath.is_symlink() or not sitepath.is_dir() or not SITE_RE.fullmatch(sitepath.name):
                continue
            site = sitepath.name
            for path in walk(sitepath / 'subset_data'):
                relative = str(path.relative_to(sitepath / 'subset_data'))
                if (path.suffix.lower() in {'.nc', '.nc4', '.grib', '.grb', '.grb2'}
                        and ERA_RE.search(relative) and not STATIC_RE.search(relative)):
                    add(path, 'surface_ERA_cache', site)
                elif LOG_RE.search(path.name):
                    add(path, 'old_log', site)
            for path in topfiles(sitepath):
                if LOG_RE.search(path.name):
                    add(path, 'old_log', site)
    for path in topfiles(home):
        if LOG_RE.search(path.name):
            add(path, 'old_log')
    for folder in sorted(home.iterdir()):
        if (folder.is_dir() and not folder.is_symlink()
                and folder.name.startswith(('ameriflux_', 'AmeriFlux_', 'Batch', 'CLM_FATES_maintenance'))):
            for path in topfiles(folder):
                if LOG_RE.search(path.name):
                    add(path, 'old_log')
    casesroot = scratch / 'CLM_FATES' / 'cases'
    if casesroot.is_dir():
        for case in sorted(casesroot.iterdir()):
            if case.is_dir() and not case.is_symlink():
                for path in topfiles(case):
                    if LOG_RE.search(path.name):
                        add(path, 'old_log', case=case.name)
                for path in walk(case / 'logs'):
                    if LOG_RE.search(path.name):
                        add(path, 'old_log', case=case.name)
    # Archived logs only. Never descend into active run, history, rest, or bld.
    arc = scratch / 'CTSM_FATES_OUTPUTS' / 'archive'
    if arc.is_dir():
        for case in sorted(arc.iterdir()):
            if case.is_dir() and not case.is_symlink():
                for path in walk(case / 'logs'):
                    if LOG_RE.search(path.name):
                        add(path, 'old_log', case=case.name)
    return list(rows.values())


def references(rows, scratch, home):
    """Conservative literal-reference scan; not a proof of all possible readers.

    Match exact files AND directory references in configurations/receipts.
    Generic project root constants do not count as references to every file.
    Long metadata files stream in chunks rather than being silently omitted.
    """
    lookup = collections.defaultdict(set)
    for row in rows:
        path = Path(row['path'])
        keys = {str(path), path.name}
        if row['kind'] == 'surface_ERA_cache':
            subset = scratch / 'CLM_FATES' / 'sites' / 'MY_EC_SITE' / row['site'] / 'subset_data'
            for parent in path.parents:
                keys.add(str(parent))
                if parent == subset:
                    break
        for value in keys:
            lookup[value].add(row['path'])
            if value.startswith('/iridisfs/scratch/'):
                lookup[value.replace('/iridisfs/scratch/', '/scratch/', 1)].add(row['path'])
            if value.startswith('/iridisfs/home/'):
                lookup[value.replace('/iridisfs/home/', '/home/', 1)].add(row['path'])
    refs, configured = {}, collections.defaultdict(set)
    total = 0
    for meta in metadata(scratch, home):
        total += 1
        if total % 500 == 0:
            print('SCANNING_METADATA files={}'.format(total), flush=True)
        with meta.open('r', encoding='utf-8', errors='replace') as handle:
            carry = ''
            while True:
                chunk = handle.read(256 * 1024)
                if not chunk:
                    break
                text = carry + chunk
                # Tokens include absolute paths, basenames and quoted fields.
                tokens = re.findall(r'[^\s\x00\x01-\x1f\"\'<>|,;\[\]{}()=]+', text)
                for token in tokens:
                    token = token.rstrip(':')
                    for key in {token, token.rsplit('/', 1)[-1]}:
                        for target in lookup.get(key, ()):
                            refs.setdefault(target, str(meta))
                if meta.name in {'lnd_in', 'user_nl_clm'}:
                    for value in re.findall(r'\bfsurdat\s*=\s*[\"\']([^\"\']+)[\"\']', text, re.I):
                        candidate = Path(value)
                        if candidate.is_absolute():
                            candidate = candidate.resolve()
                            if surface(candidate):
                                # Match configured surface path to its source site subtree.
                                for part in candidate.parts:
                                    if SITE_RE.fullmatch(part):
                                        configured[part].add(str(candidate))
                                # A custom surface outside the site tree is evidence only
                                # for the exact case-name site, not every station.
                                match = re.match(r'^([A-Z]{2}-[A-Za-z0-9]+)_', meta.parent.parent.name)
                                if match:
                                    configured[match.group(1)].add(str(candidate))
                carry = text[-16384:]
    return refs, configured, total


def audit(scratch, home, min_days, jobs):
    rows = discover(scratch, home)
    refs, configured, scanned = references(rows, scratch, home)
    protected, busy_sites = active_paths(jobs, scratch)
    cutoff = time.time_ns() - int(min_days * 86400 * 1e9)
    for row in rows:
        reasons = []
        if row['signature'][3] > cutoff:
            reasons.append('younger_than_{}d'.format(min_days))
        if active(row, protected, busy_sites):
            reasons.append('queued_or_running_job')
        if row['path'] in refs:
            reasons.append('metadata_reference:' + refs[row['path']])
        if row['kind'] == 'surface_ERA_cache':
            proof = sorted(configured.get(row['site'], ()))
            row['configured_surface_files'] = proof
            if not proof:
                reasons.append('no_site_surface_verified_in_case_configuration')
        row['reasons'] = reasons
        row['eligible'] = not reasons
    print('MODE=AUDIT VERSION={} metadata_files={} queued_jobs={}'.format(VERSION, scanned, len(jobs)), flush=True)
    for kind in ('surface_ERA_cache', 'old_log'):
        for eligible in (True, False):
            group = [r for r in rows if r['kind'] == kind and r['eligible'] == eligible]
            print('{} {} files={} GiB={:.3f}'.format(kind, 'ELIGIBLE' if eligible else 'KEEP',
                  len(group), sum(r['bytes'] for r in group) / 1024**3), flush=True)
    print('Original ERA/ERA5-Land source evidence, forcing, surface, restart/history and Git: PROTECTED', flush=True)
    return rows


def verify_session(session):
    manifest_path = session / 'manifest.json'
    require(ordinary(manifest_path) is not None, 'Missing/unsafe manifest')
    doc = json.loads(manifest_path.read_text())
    require(doc.get('version') == VERSION and doc.get('verified_archive') is True,
            'Session has not completed archive verification')
    archive = session / 'files.tar.gz'
    require(ordinary(archive) is not None and digest(archive) == doc['archive_sha256'],
            'Archive SHA256 mismatch')
    members = {r['member']: r for r in doc['files']}
    require(len(members) == len(doc['files']), 'Duplicate archived member')
    with tarfile.open(str(archive), 'r:gz') as tar:
        seen = set()
        for member in tar:
            require(member.isfile() and member.name in members and member.name not in seen,
                    'Unexpected archive member: ' + member.name)
            row = members[member.name]
            h = hashlib.sha256()
            stream = tar.extractfile(member)
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(chunk)
            require(member.size == row['bytes'] and h.hexdigest() == row['sha256'],
                    'Archive member verification failed: ' + member.name)
            seen.add(member.name)
    require(seen == set(members), 'Missing archive members')
    return doc


def nfs_backup(path, scratch, home):
    # Trigger the existing automount parent, then check effective filesystem.
    require(path.parent.is_dir(), 'Research Filestore parent unavailable: ' + str(path.parent))
    effective = Path(path).resolve()
    require(effective != scratch and scratch not in effective.parents and
            effective != home and home not in effective.parents, 'Backup cannot be on Scratch/Home')
    require(command(['findmnt', '-n', '-o', 'FSTYPE', '-T', str(path.parent)]).strip() in {'nfs', 'nfs4'},
            'Research Filestore is not an NFS mount; no archive or deletion')
    path.mkdir(exist_ok=True)
    require(command(['findmnt', '-n', '-o', 'FSTYPE', '-T', str(path)]).strip() in {'nfs', 'nfs4'},
            'Backup destination lost NFS mount')


def archive_rows(rows, backup):
    session = backup / ('scratch_cache_logs_' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '_' + uuid.uuid4().hex[:8])
    session.mkdir()
    saved = []
    partial = session / 'files.tar.gz.partial'
    with tarfile.open(str(partial), 'w:gz', compresslevel=1, dereference=False) as tar:
        for i, row in enumerate(rows, 1):
            path = Path(row['path'])
            require(ordinary(path) == row['signature'], 'File changed before archive: ' + str(path))
            row = dict(row, member=str(path).lstrip('/'), sha256=digest(path))
            tar.add(str(path), arcname=row['member'], recursive=False)
            require(ordinary(path) == row['signature'], 'File changed during archive: ' + str(path))
            saved.append(row)
            if i % 100 == 0:
                print('ARCHIVING {}/{}'.format(i, len(rows)), flush=True)
    archive = session / 'files.tar.gz'
    partial.rename(archive)
    doc = dict(version=VERSION, verified_archive=True, archive_sha256=digest(archive), files=saved)
    (session / 'manifest.json').write_text(json.dumps(doc, indent=2) + '\n')
    verify_session(session)
    print('VERIFIED_BACKUP', session, flush=True)
    return session, doc


def prune(doc, session, current_rows, jobs, scratch):
    # Reaudit after a potentially long archive; exclude new refs, new jobs,
    # changed eligibility, and all changes in file identity/content.
    permitted = {r['path']: r for r in current_rows if r['eligible']}
    protected, busy_sites = active_paths(jobs, scratch)
    removed, nominal = 0, 0
    actions = session / 'cleanup_actions.jsonl'
    last_queue = time.monotonic()
    with actions.open('a') as log:
        for row in doc['files']:
            if time.monotonic() - last_queue >= 15:
                protected, busy_sites = active_paths(queue(), scratch)
                last_queue = time.monotonic()
            path = Path(row['path'])
            status = 'KEEP'
            if (str(path) in permitted and not active(row, protected, busy_sites)
                    and ordinary(path) == row['signature'] and digest(path) == row['sha256']):
                require(ordinary(path) == row['signature'], 'Source changed at unlink: ' + str(path))
                path.unlink()
                removed += 1
                nominal += row['bytes']
                status = 'REMOVED_AFTER_SHA_VERIFIED_BACKUP'
            log.write(json.dumps(dict(path=str(path), status=status)) + '\n')
            log.flush()
    print('CLEAN_COMPLETE removed_files={} nominal_GiB={:.3f} receipt={}'.format(
        removed, nominal / 1024**3, actions), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('audit', 'clean', 'verify'))
    p.add_argument('--min-days', type=float, default=7, help='Minimum age; cannot be below 7 days')
    p.add_argument('--details', action='store_true')
    p.add_argument('--confirm', default='')
    p.add_argument('--session', type=Path)
    args = p.parse_args()
    require(args.min_days >= 7, 'Minimum retention is seven days')
    if args.mode == 'verify':
        require(args.session is not None, '--session is required')
        verify_session(args.session.resolve(strict=True))
        print('VERIFIED_SESSION', args.session)
        return
    scratch, home = SCRATCH.resolve(strict=True), HOME.resolve(strict=True)
    rows = audit(scratch, home, args.min_days, queue())
    for row in rows:
        if args.details or row['eligible']:
            print('{}\t{}\t{}\t{}\t{}'.format('ELIGIBLE' if row['eligible'] else 'KEEP',
                  row['kind'], row['bytes'], row['path'], ';'.join(row['reasons'])), flush=True)
    candidates = [r for r in rows if r['eligible']]
    if args.mode == 'audit':
        print('READONLY_AUDIT_COMPLETE nothing written/deleted; eligible_files={}'.format(len(candidates)))
        return
    require(args.confirm == TOKEN, 'clean requires --confirm ' + TOKEN)
    if not candidates:
        print('NOTHING_ELIGIBLE; no archive or deletion')
        return
    nfs_backup(BACKUP, scratch, home)
    # Separate maintenance runs cannot archive/delete simultaneously.
    with (BACKUP / '.scratch_cache_log_cleanup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rows = audit(scratch, home, args.min_days, queue())
        candidates = [r for r in rows if r['eligible']]
        if not candidates:
            print('NOTHING_ELIGIBLE_AFTER_RECHECK')
            return
        session, doc = archive_rows(candidates, BACKUP)
        fresh = audit(scratch, home, args.min_days, queue())
        doc = verify_session(session)
        prune(doc, session, fresh, queue(), scratch)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        print('CLEANUP_STOP:', error, file=sys.stderr, flush=True)
        raise SystemExit(2)
