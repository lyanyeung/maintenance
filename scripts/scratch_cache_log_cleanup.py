#!/usr/bin/env python3
"""IRIDIS6 ERA5 cleanup, KEEP AmeriFlux Batch03 and Batch05; Python 3.9+.

audit: read-only. clean: archive on NFS, verify every member, then unlink
unchanged eligible files. Retain Batch03/05 ERA sources, shared dependencies,
runtime inputs, EC observations, scientific receipts, histories and restarts.
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

VERSION = '20261027-v3-mount-probe'
TOKEN = 'ARCHIVE_VERIFY_CLEAN_ERA_EXCEPT_AMF03_05_AND_OLD_LOGS'
SCRATCH = Path('/iridisfs/scratch/ly3n24')
HOME = Path('/iridisfs/home/ly3n24')
BACKUP = Path('/research/xclimatetbm/ly3n24/Scratch_Declutter')
SITE_RE = re.compile(r'^[A-Z]{2}-[A-Za-z0-9]+$')
ERA_RE = re.compile(r'era[-_]?5(?:[-_]?land)?', re.I)
STATIC_RE = re.compile(r'surfdata|domain|mesh|landuse|pft|snow|soil', re.I)
LOG_RE = re.compile(r'(?:\.log(?:\.[A-Za-z0-9_-]+)*|\.(?:out|err))(?:\.gz)?$', re.I)
META_SUFFIXES = {'.json', '.xml', '.nml', '.streams', '.cfg', '.yaml', '.yml', '.tsv',
                 '.txt', '.sha256', '.py', '.sh'}
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', 'history', 'hist', 'rest', 'restart',
             'raw', 'packages', 'inputdata', 'bld'}
# Pinned from the workflow repository's exact site manifests on 27 Oct 2026:
# Batch03 Git blob 10cfac044cfe8d01d7a8033c3292cb3c2ee3e5c1
# Batch05 Git blob 27f138fdb1dd91c3d8ef8e4b70bb5f833643f9c3
PROTECTED_SITES = frozenset((
    'US-Kon', 'US-Me2', 'US-Me6', 'US-MMS', 'US-MOz', 'US-Mpj', 'US-MtB',
    'US-NC2', 'US-NR1', 'US-NR3',
    'US-UiD', 'US-UMB', 'US-UMd', 'US-Var', 'US-Vcm', 'US-Vcp', 'US-WCr',
    'US-Whs', 'US-Wjs', 'US-Wkg'))
PROTECTED_SITE_RE = re.compile(r'(?<![A-Za-z0-9])(?:' + '|'.join(
    re.escape(s) for s in sorted(PROTECTED_SITES)) + r')(?![A-Za-z0-9])', re.I)
PROTECTED_BATCH_RE = re.compile(r'(?:ameriflux|amf).*batch0?[35](?:[^0-9]|$)', re.I)
PAYLOAD_SUFFIXES = {'.nc', '.nc4', '.grib', '.grb', '.grb2', '.zip', '.csv', '.json'}
RECORD_RE = re.compile(r'receipt|manifest|(?<![a-z])sha(?:256)?(?![a-z])|checksum|request|metadata|summary|'
                       r'catalog|decision|ledger|report|plan|approval|license|readme|provenance|inventory|audit|status', re.I)
OBS_RE = re.compile(r'FULLSET|FLUXMET|FLUXES|BIF', re.I)


def protected_scope(path):
    text = str(path)
    return bool(PROTECTED_SITE_RE.search(text) or any(
        PROTECTED_BATCH_RE.search(part) for part in Path(text).parts))


def runtime_metadata(path):
    return (path.name.lower() in {'lnd_in', 'datm_in', 'user_nl_clm', 'user_nl_datm'}
            or 'streams' in path.name.lower() or 'CaseDocs' in path.parts
            or 'Buildconf' in path.parts)


def era_payload(path, relative):
    # Retain mixed original EC packages/products even in an ERA-labelled folder.
    return (path.suffix.lower() in PAYLOAD_SUFFIXES and ERA_RE.search(str(relative))
            and not STATIC_RE.search(path.name) and not RECORD_RE.search(path.name)
            and not OBS_RE.search(path.name) and not (
                path.suffix.lower() == '.zip' and path.name.startswith(('AMF_', 'FLX_'))
                and not ERA_RE.search(path.name)))


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
    if row['kind'] == 'ERA5_source_or_cache' and any(re.search(
            r'(?<![A-Za-z0-9])' + re.escape(site) + r'(?![A-Za-z0-9])', row['path'], re.I)
            for site in sites):
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


def discover(scratch, home):
    rows = {}
    def add(path, kind, site='', case=''):
        sig = ordinary(path)
        if sig is not None:
            rows[str(path)] = dict(path=str(path), kind=kind, site=site, case=case,
                                   signature=sig, bytes=sig[2])
    def source_cache(folder, site=''):
        # Only source/cache stores, never science_trials, selected forcing or DATM.
        for path in walk(folder, (SKIP_DIRS - {'raw'}) | {'science_trials', 'reports', 'datm',
                                          'processed', 'selected', 'forcing_final'}):
            if era_payload(path, str(folder.name) + '/' + str(path.relative_to(folder))):
                add(path, 'ERA5_source_or_cache', site)
    sitesroot = scratch / 'CLM_FATES' / 'sites' / 'MY_EC_SITE'
    if sitesroot.is_dir():
        for sitepath in sorted(sitesroot.iterdir()):
            if sitepath.is_symlink() or not sitepath.is_dir() or not SITE_RE.fullmatch(sitepath.name):
                continue
            site = sitepath.name
            for path in walk(sitepath / 'subset_data'):
                relative = str(path.relative_to(sitepath / 'subset_data'))
                if era_payload(path, relative):
                    add(path, 'ERA5_source_or_cache', site)
                elif LOG_RE.search(path.name):
                    add(path, 'old_log', site)
            for path in topfiles(sitepath):
                if LOG_RE.search(path.name):
                    add(path, 'old_log', site)
                elif era_payload(path, path.name):
                    add(path, 'ERA5_source_or_cache', site)
            # Original standalone ERA5 HH companions are now authorised for
            # OTHER sites; Batch03/05 sites are unconditionally protected below.
            forcing = sitepath / 'forcing'
            if forcing.is_dir() and not forcing.is_symlink():
                for folder in sorted(forcing.iterdir()):
                    if folder.is_dir() and not folder.is_symlink() and (
                            folder.name.lower().startswith('raw') or
                            ERA_RE.search(folder.name) or folder.name.lower() in {'cache', 'external_evidence'}):
                        source_cache(folder, site)
            for folder in sorted(sitepath.iterdir()):
                if folder.is_dir() and not folder.is_symlink() and ERA_RE.search(folder.name):
                    source_cache(folder, site)
    experiments = scratch / 'CLM_FATES' / 'experiments'
    if experiments.is_dir() and not experiments.is_symlink():
        for experiment in sorted(experiments.iterdir()):
            if not experiment.is_dir() or experiment.is_symlink():
                continue
            for folder in sorted(experiment.iterdir()):
                if folder.is_dir() and not folder.is_symlink() and (
                        folder.name.lower() in {'external_evidence', 'external_data', 'cache', 'cds'}
                        or (ERA_RE.search(folder.name) and not re.search(
                            r'trial|shadow|forcing|selected|final|datm|report', folder.name, re.I))):
                    source_cache(folder)
    for path in topfiles(home):
        if LOG_RE.search(path.name):
            add(path, 'old_log')
    for folder in sorted(home.iterdir()):
        if (folder.is_dir() and not folder.is_symlink()
                and folder.name.startswith(('ameriflux_', 'AmeriFlux_', 'Batch', 'CLM_FATES_maintenance'))):
            for path in topfiles(folder):
                if LOG_RE.search(path.name):
                    add(path, 'old_log')
        if folder.is_dir() and not folder.is_symlink() and ERA_RE.search(folder.name):
            source_cache(folder)
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


def protected_link_targets(scratch, home):
    """Protect shared real files reached through a Batch03/05 symlink."""
    sites = scratch / 'CLM_FATES' / 'sites' / 'MY_EC_SITE'
    roots = [sites / site for site in PROTECTED_SITES]
    for base in (scratch / 'CLM_FATES' / 'experiments', home):
        if base.is_dir():
            roots.extend(p for p in base.iterdir() if protected_scope(p))
    targets = set()
    def error(exc):
        raise exc
    for root in roots:
        if root.is_symlink():
            targets.add(str(root.resolve()))
            continue
        if not root.is_dir():
            continue
        for folder, dirs, files in os.walk(str(root), followlinks=False, onerror=error):
            for name in dirs + files:
                path = Path(folder) / name
                if path.is_symlink():
                    targets.add(str(path.resolve()))
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in (SKIP_DIRS - {'raw', 'packages'})
                       and not (Path(folder) / d).is_symlink()]
    return targets


def references(rows, scratch, home):
    """Conservative literal-reference scan; not a proof of all possible readers.

    Match exact files AND directory references in configurations/receipts.
    Generic project root constants do not count as references to every file.
    Long metadata files stream in chunks rather than being silently omitted.
    """
    lookup = collections.defaultdict(set)
    kinds = {r['path']: r['kind'] for r in rows}
    for row in rows:
        path = Path(row['path'])
        keys = {str(path), path.name}
        if row['kind'] == 'ERA5_source_or_cache':
            for parent in path.parents:
                if (parent in {scratch, home} or parent.name == row['site'] or
                        parent.parent == scratch / 'CLM_FATES' / 'experiments'):
                    break
                keys.add(str(parent))
        for value in keys:
            lookup[value].add(row['path'])
            if value.startswith('/iridisfs/scratch/'):
                lookup[value.replace('/iridisfs/scratch/', '/scratch/', 1)].add(row['path'])
            if value.startswith('/iridisfs/home/'):
                lookup[value.replace('/iridisfs/home/', '/home/', 1)].add(row['path'])
    refs, critical_refs = {}, {}
    found_by_meta, edges, critical = {}, {}, set()
    total = 0
    for meta in metadata(scratch, home):
        total += 1
        if total % 500 == 0:
            print('SCANNING_METADATA files={}'.format(total), flush=True)
        meta_key = str(meta.resolve())
        found_by_meta[meta_key], edges[meta_key] = set(), set()
        if protected_scope(meta) or runtime_metadata(meta):
            critical.add(meta_key)
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
                    contextual = os.path.normpath(token if token.startswith('/') else str(meta.parent / token))
                    for key in {token, contextual}:
                        for target in lookup.get(key, ()):
                            # A bare ERA filename is relative to its metadata
                            # directory; it is not every identically named file
                            # in every other batch's download store.
                            if key == token and not token.startswith('/') and kinds[target] != 'old_log':
                                continue
                            refs.setdefault(target, str(meta))
                            found_by_meta[meta_key].add(target)
                    for target in lookup.get(token.rsplit('/', 1)[-1], ()):
                        if kinds[target] == 'old_log':
                            refs.setdefault(target, str(meta))
                            found_by_meta[meta_key].add(target)
                    if token.startswith('/') and (Path(token).suffix.lower() in META_SUFFIXES | {'.csv'}
                                                   or Path(token).name.lower() == 'sha256sums'):
                        edges[meta_key].add(str(Path(token).resolve()))
                carry = text[-16384:]
    # Follow literal receipt/source links out of protected batches, preserving
    # shared dependencies even when their on-disk folder belongs to another batch.
    pending, seen = list(critical), set()
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        for target in found_by_meta.get(key, ()):
            critical_refs.setdefault(target, key)
        pending.extend(edges.get(key, set()) - seen)
    return refs, critical_refs, total


def audit(scratch, home, min_days, jobs):
    rows = discover(scratch, home)
    refs, critical_refs, scanned = references(rows, scratch, home)
    aliases = protected_link_targets(scratch, home)
    protected, busy_sites = active_paths(jobs, scratch)
    cutoff = time.time_ns() - int(min_days * 86400 * 1e9)
    for row in rows:
        reasons = []
        if row['kind'] == 'old_log' and row['signature'][3] > cutoff:
            reasons.append('younger_than_{}d'.format(min_days))
        if active(row, protected, busy_sites):
            reasons.append('queued_or_running_job')
        if row['kind'] == 'old_log' and row['path'] in refs:
            reasons.append('metadata_reference:' + refs[row['path']])
        if row['kind'] == 'ERA5_source_or_cache':
            if protected_scope(row['path']):
                reasons.append('KEEP_AMERIFLUX_BATCH03_OR_BATCH05')
            if any(Path(row['path']) == Path(t) or Path(t) in Path(row['path']).parents for t in aliases):
                reasons.append('SHARED_WITH_BATCH03_OR_BATCH05_VIA_SYMLINK')
            if row['path'] in critical_refs:
                reasons.append('protected_batch_or_runtime_reference:' + critical_refs[row['path']])
            row['historical_reference_restore_if_needed'] = refs.get(row['path'])
        row['reasons'] = reasons
        row['eligible'] = not reasons
    print('MODE=AUDIT VERSION={} metadata_files={} queued_jobs={}'.format(VERSION, scanned, len(jobs)), flush=True)
    print('ERA_POLICY=KEEP_AMF_BATCH03_BATCH05_AND_SHARED_DEPENDENCIES; OTHER_ERA5_ARCHIVE_THEN_REMOVE', flush=True)
    for kind in ('ERA5_source_or_cache', 'old_log'):
        for eligible in (True, False):
            group = [r for r in rows if r['kind'] == kind and r['eligible'] == eligible]
            print('{} {} files={} GiB={:.3f}'.format(kind, 'ELIGIBLE' if eligible else 'KEEP',
                  len(group), sum(r['bytes'] for r in group) / 1024**3), flush=True)
    print('Batch03/05 ERA, runtime inputs, EC observations, derived forcing, scientific receipts, surface, restart/history and Git: PROTECTED', flush=True)
    return rows


def verify_session(session):
    manifest_path = session / 'manifest.json'
    require(ordinary(manifest_path) is not None, 'Missing/unsafe manifest')
    doc = json.loads(manifest_path.read_text())
    require(doc.get('version') in {'20261027-v1', '20261027-v2', VERSION} and doc.get('verified_archive') is True,
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


def mount_info(path):
    """Parse structured findmnt output; do not conflate an autofs parent with NFS."""
    resolved = Path(path).resolve(strict=True)
    doc = json.loads(command(['findmnt', '--json', '--target', str(resolved),
                              '--output', 'TARGET,SOURCE,FSTYPE']))
    rows = []
    def visit(items):
        for item in items:
            if all(item.get(k) for k in ('target', 'source', 'fstype')):
                target = Path(item['target'])
                if target == resolved or target in resolved.parents:
                    rows.append({k: item[k] for k in ('target', 'source', 'fstype')})
            visit(item.get('children', []))
    visit(doc.get('filesystems', []))
    require(rows, 'No effective mount record for ' + str(resolved))
    # A mounted real filesystem may shadow an autofs trigger at the same path.
    real = [row for row in rows if row['fstype'] != 'autofs']
    candidates = real or rows
    deepest = max(len(Path(row['target']).parts) for row in candidates)
    selected = [row for row in candidates if len(Path(row['target']).parts) == deepest]
    unique = {(r['target'], r['source'], r['fstype']) for r in selected}
    require(len(unique) == 1, 'Ambiguous effective backup mount: ' + json.dumps(selected))
    return selected[0]


def probe_backup(path):
    parent = Path(path).parent
    require(parent.is_dir(), 'Research Filestore parent unavailable: ' + str(parent))
    info = mount_info(parent)
    print('BACKUP_MOUNT_OBSERVED', json.dumps(dict(path=str(parent),
          resolved=str(parent.resolve()), **info)), flush=True)
    return info


def require_nfs(info):
    require(info['fstype'] in {'nfs', 'nfs4'} and info['target'] != '/',
            'Backup mount needs review: ' + json.dumps(info) +
            '; no archive or deletion. Use probe-mount to report the actual filesystem; '
            'do not bypass this check')


def nfs_backup(path, scratch, home):
    # Trigger the existing automount parent, then check effective filesystem.
    require(path.parent.is_dir(), 'Research Filestore parent unavailable: ' + str(path.parent))
    effective = Path(path).resolve()
    require(effective != scratch and scratch not in effective.parents and
            effective != home and home not in effective.parents, 'Backup cannot be on Scratch/Home')
    before = probe_backup(path)
    require_nfs(before)
    path.mkdir(exist_ok=True)
    after = mount_info(path)
    require_nfs(after)
    require(before == after, 'Backup mount changed while preparing archive directory')


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
    p.add_argument('mode', choices=('audit', 'clean', 'verify', 'probe-mount'))
    p.add_argument('--min-days', type=float, default=7, help='Minimum log age (>=7 days); no age gate on other ERA5')
    p.add_argument('--details', action='store_true')
    p.add_argument('--confirm', default='')
    p.add_argument('--session', type=Path)
    args = p.parse_args()
    require(args.min_days >= 7, 'Minimum retention is seven days')
    if args.mode == 'probe-mount':
        probe_backup(BACKUP)
        print('READONLY_MOUNT_PROBE_COMPLETE no writes/deletion; no Slurm query')
        return
    if args.mode == 'verify':
        require(args.session is not None, '--session is required')
        verify_session(args.session.resolve(strict=True))
        print('VERIFIED_SESSION', args.session)
        return
    scratch, home = SCRATCH.resolve(strict=True), HOME.resolve(strict=True)
    if args.mode == 'clean':
        require(args.confirm == TOKEN, 'clean requires --confirm ' + TOKEN)
        require_nfs(probe_backup(BACKUP))
    rows = audit(scratch, home, args.min_days, queue())
    for row in rows:
        if args.details:
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
