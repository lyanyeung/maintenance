#!/usr/bin/env python3
"""Audit/pack only the 38 current CIME Git repositories of four ICOS batches.

Python 3.9+, Linux/IRIDIS6. Default audit is read-only. No model output paths
are opened. Unrelated Slurm jobs require consistent private-case paths.
"""
import argparse
from contextlib import contextmanager
import getpass
import hashlib
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
from datetime import datetime, timezone

VERSION = '20261026-v2'

# Pinned to maintenance/check_four_spinups.py at repository tree
# f370e4ea7c8c30210fde3dcf57b152d1b2643967 (26 October 2026).
# Case suffixes are names, not proof of elapsed model years.
BATCHES = {
    '6': 'DE-Hai RU-Fyo DE-Tha DK-Sor NL-Loo IL-Yat'.split(),
    '8': 'CZ-BK1 IT-Ren FR-Hes FR-FBn DE-Hzd CH-Dav BE-Vie IT-Lav'.split(),
    '11': 'BE-Bra FR-Fon GL-ZaH IT-Noe AT-Neu FI-Sod CH-Aws CH-Cha CH-Fru CH-Lae CZ-Stn'.split(),
    '13': 'DE-Lnf DE-Obe ES-Agu ES-LJu IT-Col DE-Gri DE-RuR FI-Hyy FI-Let FR-Pue GF-Guy IT-MBo IT-Tor'.split(),
}
OVERRIDES = {
    'IT-Noe': 'IT-Noe_ERA5_ABW_BRANCH2009',
    'CH-Aws': 'CH-Aws_FATES_SPINUP1000_RH42A_V8_CONT2016',
}
CASES = {}
for batch, sites in BATCHES.items():
    suffix = '_FATES_SPINUP500_11P' if batch == '11' else '_FATES_SPINUP1000_S30SHARED' if batch == '13' else '_FATES_SPINUP500'
    for site in sites:
        CASES[OVERRIDES.get(site, site + suffix)] = batch


def command(args):
    env = os.environ.copy()
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        env.pop(key, None)
    env['GIT_OPTIONAL_LOCKS'] = '0'
    result = subprocess.run([str(a) for a in args], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('{}: {}'.format(' '.join(map(str, args)),
                           result.stderr.decode(errors='replace').strip()))
    return result.stdout


def git(case, *args):
    return command(['git', '-C', case, *args])


def queue():
    raw = command(['squeue', '-h', '-u', getpass.getuser(), '-o', '%i|%j|%T|%Z|%o'])
    own = os.environ.get('SLURM_JOB_ID', '')
    jobs = []
    for line in raw.decode().splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split('|')]
        if len(parts) != 5 or not all(parts):
            raise RuntimeError('Unrecognized squeue output: ' + line)
        if parts[0].strip() != own:
            jobs.append(dict(zip(('id', 'name', 'state', 'workdir', 'command'), parts)))
    return jobs


def job_relation(root, names, job):
    if job['name'] in {p + n for n in names for p in ('run.', 'st_archive.')}:
        return 'BLOCK_SELECTED_CASE'
    match = re.fullmatch(r'(run|st_archive)\.([A-Za-z0-9_.-]+)', job['name'])
    if not match:
        return 'BLOCK_UNKNOWN_JOB'
    phase, name = match.groups()
    if name in ('.', '..'):
        return 'BLOCK_INVALID_CASE'
    try:
        other, gd = case_git(root, name)
        selected = {(root / n).resolve() for n in names}
        if other in selected or gd in {(p / '.git').resolve() for p in selected}:
            return 'BLOCK_SHARED_CASE'
        workdir = Path(job['workdir']).resolve(strict=True)
        words = shlex.split(job['command'])
        if not words:
            return 'BLOCK_UNKNOWN_COMMAND'
        executable = Path(words[0])
        executable = (executable if executable.is_absolute() else workdir / executable).resolve(strict=True)
        if workdir != other or executable.parent != other:
            return 'BLOCK_PATH_MISMATCH'
        if executable.name not in ('.case.' + phase, 'case.' + phase):
            return 'BLOCK_UNKNOWN_COMMAND'
        if not executable.is_file():
            return 'BLOCK_UNKNOWN_COMMAND'
    except (OSError, RuntimeError, ValueError):
        return 'BLOCK_UNVERIFIED_PATH'
    return 'UNRELATED_PRIVATE_CASE'


def require_idle(root, names):
    blocked = [(job, job_relation(root, names, job)) for job in queue()]
    blocked = [(job, relation) for job, relation in blocked if relation != 'UNRELATED_PRIVATE_CASE']
    if blocked:
        raise RuntimeError('Slurm overlap or unverified job; no packing.\n' +
                           '\n'.join('{} {} {}'.format(j['id'], j['name'], r) for j, r in blocked))


class GitLocks(RuntimeError):
    pass


def inventory(gitdir):
    inodes = set()
    locks = []
    def fail(error):
        raise error
    for folder, dirs, files in os.walk(gitdir, followlinks=False, onerror=fail):
        for path in [Path(folder)] + [Path(folder) / x for x in files + dirs]:
            if path.is_symlink():
                raise RuntimeError('Symlink inside .git needs separate review: ' + str(path))
            stat = path.lstat()
            inodes.add((stat.st_dev, stat.st_ino))
            if path.name == 'gc.pid' or path.name.endswith('.lock'):
                locks.append(str(path))
    return len(inodes), sorted(set(locks))


def case_git(root, name):
    case = root / name
    if case.is_symlink() or not case.is_dir() or case.resolve() != case:
        raise RuntimeError('Missing case or symlink case path: ' + str(case))
    gd = case / '.git'
    if gd.is_symlink() or not gd.is_dir() or gd.resolve() != gd:
        raise RuntimeError('Expected a private .git directory: ' + str(gd))
    if hasattr(os, 'getuid') and gd.stat().st_uid != os.getuid():
        raise RuntimeError('Case Git is owned by another user: ' + str(gd))
    if not (case / 'CaseStatus').is_file() or not (case / 'env_run.xml').is_file():
        raise RuntimeError('Missing CIME CaseStatus/env_run.xml: ' + str(case))
    top = Path(os.fsdecode(git(case, 'rev-parse', '--show-toplevel')).strip()).resolve()
    common = Path(os.fsdecode(git(case, 'rev-parse', '--git-common-dir')).strip())
    common = common if common.is_absolute() else case / common
    if top != case or common.resolve() != gd:
        raise RuntimeError('Git root/shared worktree mismatch: ' + name)
    if (gd / 'objects').is_symlink() or (gd / 'objects/info/alternates').exists() or (gd / 'modules').exists():
        raise RuntimeError('Shared object storage/submodules need separate review: ' + name)
    return case, gd


def inspect(root, name):
    case, gd = case_git(root, name)
    inodes, locks = inventory(gd)
    if locks:
        raise GitLocks('Git locks present; never remove automatically: ' + ', '.join(locks))
    git(case, 'fsck', '--connectivity-only', '--no-dangling')
    counts = {}
    for line in git(case, 'count-objects', '-v').decode().splitlines():
        key, _, value = line.partition(': ')
        if key in ('count', 'in-pack', 'packs'):
            counts[key] = int(value)
    if set(counts) != {'count', 'in-pack', 'packs'}:
        raise RuntimeError('Incomplete Git object statistics: ' + name)
    return case, inodes, counts


def snapshot(case):
    # Include unreachable objects, staged changes, dirty/untracked status,
    # stash/other refs and reflogs. No index refresh or working-tree writes.
    objects = sorted(git(case, 'cat-file', '--batch-all-objects',
                         '--batch-check=%(objectname)').splitlines())
    index = case / '.git/index'
    return {
        'HEAD': git(case, 'rev-parse', 'HEAD'),
        'refs': git(case, 'for-each-ref', '--format=%(refname) %(objectname)'),
        'reflogs': git(case, 'reflog', 'show', '--all', '--format=%gD %H %gs'),
        'index': hashlib.sha256(index.read_bytes()).digest() if index.exists() else None,
        'status': git(case, 'status', '--porcelain=v1', '-z', '--untracked-files=all'),
        'all_object_ids': hashlib.sha256(b'\n'.join(objects)).digest(),
    }


@contextmanager
def maintenance_lock():
    import fcntl
    # Same cross-node advisory lock as the original generic maintenance tool.
    with (Path.home() / '.cime_git_maintenance.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another maintenance invocation holds the lock') from exc
        yield


def print_stats(name, info, label=''):
    _, inodes, counts = info
    print('{}\t{}\t{}\t{}\t{}\t{}\t{}'.format(
        label, CASES[name], name, inodes, counts['count'], counts['in-pack'], counts['packs']), flush=True)


def pack(root, names, skip_locked=False):
    require_idle(root, names)
    # All selected cases must pass before the first repository is changed.
    before = {}
    skipped = []
    for name in names:
        try:
            before[name] = inspect(root, name)
        except GitLocks as exc:
            if not skip_locked:
                raise
            skipped.append(name)
            print('SKIP_LOCKED {}: {}'.format(name, exc), flush=True)
    names = list(before)
    if not names:
        print('NO_ELIGIBLE_CASES: skipped_locked={}; no repositories changed'.format(len(skipped)))
        return
    snapshots = {name: snapshot(before[name][0]) for name in names}
    total_before = sum(info[1] for info in before.values())
    total_after = 0
    for name in names:
        require_idle(root, names)
        info = inspect(root, name)
        case = info[0]
        if snapshot(case) != snapshots[name]:
            raise RuntimeError('Case changed after preflight; stopping: ' + name)
        print_stats(name, info, 'BEFORE')
        if info[2]['count']:
            # Do not expire reflogs or prune objects/worktrees. Never --force,
            # --aggressive, --prune=now or the unsupported --no-detach option.
            command(['nice', '-n', '10', 'git', '-C', case,
                     '-c', 'gc.reflogExpire=never',
                     '-c', 'gc.reflogExpireUnreachable=never',
                     '-c', 'gc.worktreePruneExpire=never',
                     '-c', 'gc.packRefs=false', '-c', 'gc.autoDetach=false',
                     '-c', 'pack.threads=1', 'gc', '--no-prune'])
        after = inspect(root, name)
        if snapshot(case) != snapshots[name]:
            raise RuntimeError('Post-pack Git state/object validation failed: ' + name)
        total_after += after[1]
        print_stats(name, after, 'AFTER')
    print('COMPLETE cases={} git_inodes_before={} git_inodes_after={} reclaimed={}'.format(
        len(names), total_before, total_after, total_before - total_after))
    print('Run myfiles for the real Scratch quota. No model output/restart files were opened.')
    if skipped:
        print('PARTIAL: skipped_locked={} cases={}; locks retained; these cases are NOT cleaned'.format(
            len(skipped), ','.join(skipped)))


def diagnose_locks(root, names):
    print('READ-ONLY LOCK DIAGNOSTICS host={} UTC={}'.format(
        socket.gethostname(), datetime.now(timezone.utc).isoformat()))
    try:
        for job in queue():
            print('JOB {} {} {} WorkDir={} Command={} relation={}'.format(
                job['id'], job['name'], job['state'], job['workdir'],
                job['command'], job_relation(root, names, job)))
    except (OSError, RuntimeError) as exc:
        print('QUEUE_UNKNOWN: ' + str(exc))
    count = 0
    for name in names:
        try:
            _, gd = case_git(root, name)
            _, locks = inventory(gd)
            if not locks:
                continue
            count += 1
            print('CASE ' + name)
            for item in locks:
                path = Path(item)
                stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                with path.open('rb') as handle:
                    contents = handle.read(2048).decode(errors='replace')
                print('LOCK {} mtime_utc={} bytes={} content={}'.format(
                    path.name, stamp, path.stat().st_size,
                    repr(contents)))
            log = gd / 'gc.log'
            if log.is_file() and not log.is_symlink():
                with log.open('rb') as handle:
                    handle.seek(max(0, log.stat().st_size - 4096))
                    print('GC_LOG_TAIL ' + repr(handle.read().decode(errors='replace')))
        except (OSError, RuntimeError) as exc:
            print('STOP {}: {}'.format(name, exc))
    try:
        raw = command(['ps', '-u', getpass.getuser(), '-o', 'pid=,ppid=,etime=,args=']).decode(errors='replace')
        for line in raw.splitlines():
            if re.search(r'\bgit(?:-gc|-repack|\s+(?:gc|repack|maintenance))\b', line):
                print('LOCAL_GIT_PROCESS ' + line.strip())
    except (OSError, RuntimeError) as exc:
        print('LOCAL_PROCESS_CHECK_UNAVAILABLE: ' + str(exc))
    print('LOCKED_CASES={}; current-host process checks cannot clear remote-host locks.'.format(count))
    print('READ ONLY: no locks moved/deleted. Age or empty contents do not prove a stale lock.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('mode', nargs='?', choices=['audit', 'run', 'locks'], default='audit')
    parser.add_argument('--skip-locked', action='store_true',
                        help='Run only: retain and report locked cases; pack other healthy cases')
    parser.add_argument('--case-root', type=Path,
                        default=Path('/iridisfs/scratch/ly3n24/CLM_FATES/cases'))
    parser.add_argument('--batch', choices=['all', *BATCHES], default='all')
    parser.add_argument('--case', action='append', choices=sorted(CASES), dest='cases',
                        help='Optionally restrict to exact current case name(s)')
    args = parser.parse_args(argv)
    try:
        root = args.case_root.resolve(strict=True)
        if not root.is_dir():
            raise RuntimeError('Not a case directory: ' + str(root))
        names = [n for n, b in CASES.items() if args.batch == 'all' or b == args.batch]
        if args.cases:
            if not set(args.cases) <= set(names):
                raise RuntimeError('--case is outside the selected batch')
            names = [n for n in names if n in args.cases]
        print('MODE={} VERSION={} ROOT={} SELECTED={} (current cases; historical trials excluded)'.format(
            args.mode, VERSION, root, len(names)), flush=True)
        print('STAGE\tBATCH\tCASE\tGIT_INODES\tLOOSE\tPACKED_OBJECTS\tPACK_FILES', flush=True)
        if args.skip_locked and args.mode != 'run':
            raise RuntimeError('--skip-locked is only valid with run')
        if args.mode == 'locks':
            diagnose_locks(root, names)
            return 0
        if args.mode == 'run':
            with maintenance_lock():
                pack(root, names, args.skip_locked)
            return 0
        try:
            jobs = queue()
            print('OTHER_SLURM_JOBS={} (unrelated jobs require verified private-case paths)'.format(len(jobs)))
            for job in jobs:
                print('JOB {} {} {} WorkDir={} Command={} relation={}'.format(
                    job['id'], job['name'], job['state'], job['workdir'],
                    job['command'], job_relation(root, names, job)))
        except (OSError, RuntimeError) as exc:
            print('QUEUE_UNKNOWN: {}; run will refuse'.format(exc))
        errors = []
        total = 0
        for name in names:
            try:
                info = inspect(root, name)
                total += info[1]
                print_stats(name, info, 'AUDIT')
            except (OSError, RuntimeError) as exc:
                errors.append(name)
                print('STOP {}: {}'.format(name, exc), flush=True)
        print('AUDIT {}: selected={} passed={} git_inodes={} failed={}'.format(
            'FAILED' if errors else 'PASSED', len(names), len(names)-len(errors),
            total, ','.join(errors) or 'none'))
        print('READ ONLY: no files created or changed by audit.')
        return int(bool(errors))
    except (OSError, RuntimeError) as exc:
        print('STOP: ' + str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
