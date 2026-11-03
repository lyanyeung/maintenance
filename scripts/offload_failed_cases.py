#!/usr/bin/env python3
"""Move Scratch outputs of FAILED first-run FIXNC cases to the Research Filestore.

Which cases: the first production runs (AD400 / NORMAL400 / AD5 smoke / superseded
input variants) of sites whose first run clearly failed (needleleaf collapse or
needleleaf loss, and the phenology problem sites). See FAILED_SITES below; override
with --sites / --exclude-site.

Never touched:
  * every site that is part of a CURRENT experiment: any site with a trial case
    (*_DIAG<n>, *_FIX<X><n>, *_PHENFIX<n>) or any ACTIVE/RECENT/locked case. The
    whole site is held, including its original cases (they are trial controls and
    clone sources);
  * any case another remaining case depends on: its path or name appears in that
    case's env_*.xml / user_nl_* / CaseDocs (EXEROOT --keepexe, RUN_REFDIR, finidat)
    or a symlink in that case's RUNDIR points into it, or ANY hop of a top-level
    symlink in that case's folder passes through it (create_clone --keepexe makes
    <clone>/SourceMods -> <source case>/SourceMods; deleting the source case leaves
    every clone's SourceMods dangling and case.run fails: "user_xml_dir
    .../SourceMods/src.drv does not exist", see docs/SOURCEMODS_DANGLING_LINKS.md);
  * the case directory itself (it is archived, but stays on Scratch for create_clone);
  * DE-Tha / NL-Loo protected examples (unless named with --sites).

Modes (one step at a time; nothing is deleted before `clean`):
  plan       READ ONLY. Select cases, resolve RUNDIR/EXEROOT/DOUT_S_ROOT, check
             ownership/overlap/dependencies, count files and bytes. Writes the plan
             JSON to ~/offload_plans/ (Home, not Scratch).
  probe-mount READ ONLY. Show the effective filesystem of the Research destination.
  archive    --plan P: re-check every case, then write ONE uncompressed tar per case to
             <dest>/<session>/ with a per-file manifest (size, mtime, SHA256) taken
             while reading, re-read the tar from the Research Filestore and compare
             every member. Writes <case>.VERIFIED only if all match. Deletes nothing.
  verify     --session S: re-read every tar and compare with its manifest (read only).
  clean      --session S --confirm DELETE_VERIFIED_SCRATCH_OUTPUTS: re-check the case
             (idle, no new dependants), then unlink only files whose size and mtime
             still equal the verified manifest; changed or new files are kept and
             reported. Removes directories only when empty. Logs every action.

Python 3.9+, IRIDIS6, no third-party modules. Run archive/clean on a node where
/research is mounted (check with probe-mount first).
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tarfile
import time
import uuid
from datetime import datetime, timezone

VERSION = '20261103-v3-sourcemods-links'
TOKEN = 'DELETE_VERIFIED_SCRATCH_OUTPUTS'
DEFAULT_DEST = Path('/research/xclimatetbm/ly3n24/Scratch_Offload')
DEFAULT_SCRATCH_ROOTS = ('/iridisfs/scratch/ly3n24', '/scratch/ly3n24')
PLAN_DIR = Path.home() / 'offload_plans'

# docs/NEEDLELEAF_COLLAPSE_DIAGNOSIS.md (Non-Competition-CLM-experiment), sections 10, 14, 16.
NEEDLELEAF_COLLAPSE = 'FI-Sod CH-Dav IL-Yat DE-Obe IT-Ren CZ-BK1 CA-Ca3 CA-LP1 US-NC2 US-GLE FI-Hyy'.split()
NEEDLELEAF_PART_LOST = ('US-Ho2 DE-Tha RU-Fyo US-Ho1 BE-Bra US-Ho3 FR-FBn FI-Let US-SP1 US-NR1 '
                        'BE-Vie US-Me2 IT-Lav US-Me6 CA-TP4').split()
PHENOLOGY = 'CZ-Stn DE-Lnf US-SRM US-SRG FR-Pue US-CMW'.split()
FAILED_SITES = NEEDLELEAF_COLLAPSE + NEEDLELEAF_PART_LOST + PHENOLOGY
PROTECTED_SITES = {'DE-Tha', 'NL-Loo'}
TRIAL_RE = re.compile(r'(?:^|_)(?:DIAG|FIX[A-Z]|PHENFIX)\d+(?=_|$)')
NOT_IDLE = {'ACTIVE', 'RECENT', 'GIT_LOCKED', 'QUEUE_UNKNOWN', 'NO_CASESTATUS'}
TEXT_FILES = ('env_case.xml', 'env_build.xml', 'env_run.xml', 'env_batch.xml', 'env_mach_specific.xml')


def load_audit():
    path = Path(__file__).resolve().parent / 'audit_pack_fixnc_case_git.py'
    spec = importlib.util.spec_from_file_location('fixnc_audit', str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


A = load_audit()


class Stop(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Stop(message)


def now_utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def site_of(name):
    m = A.NAME_RE.match(name)
    return m.group('site') if m else None


def is_within(path, root):
    return path == root or root in path.parents


# --------------------------------------------------------------------------- discovery
def case_outputs(case):
    """Resolved Scratch output roots of a case: <CIME_OUTPUT_ROOT>/<case> (run+bld) and archive."""
    env = A.read_env(case)
    raw = {k: A.expand(env.get(k), env) for k in ('RUNDIR', 'EXEROOT', 'DOUT_S_ROOT', 'RUN_REFDIR')}
    paths = {}
    for key, value in raw.items():
        if value and '$' not in value:
            paths[key] = Path(os.path.abspath(value))
    roots = []
    run, exe = paths.get('RUNDIR'), paths.get('EXEROOT')
    if run:
        roots.append(run.parent if run.parent.name == case.name else run)
    # Only a case-owned build is removed. A shared EXEROOT (e.g. one common executable in
    # another case's bld used by many --keepexe cases) is never an offload root.
    if exe and case.name in Path(os.path.realpath(str(exe))).parts \
            and not any(is_within(exe, r) for r in roots):
        roots.append(exe)
    if paths.get('DOUT_S_ROOT'):
        roots.append(paths['DOUT_S_ROOT'])
    return env, paths, roots


def dependency_text(case):
    chunks = []
    files = [case / f for f in TEXT_FILES] + sorted(case.glob('user_nl_*'))
    docs = case / 'CaseDocs'
    if docs.is_dir():
        files += sorted(p for p in docs.iterdir() if p.is_file())
    for f in files:
        try:
            if f.is_file() and f.stat().st_size < 5 * 1024 * 1024:
                chunks.append(f.read_text(errors='replace'))
        except OSError:
            pass
    return '\n'.join(chunks)


def rundir_symlink_targets(rundir):
    targets = []
    try:
        with os.scandir(str(rundir)) as it:
            for entry in it:
                if entry.is_symlink():
                    try:
                        targets.append(Path(os.path.realpath(entry.path)))
                    except OSError:
                        pass
    except OSError:
        pass
    return targets


def case_symlink_hops(case):
    """Every hop of every top-level symlink in a case folder, as absolute paths whose
    parent directory is resolved (the hop itself is NOT followed). Example:
    C/SourceMods -> B/SourceMods -> A/SourceMods gives [B/SourceMods, A/SourceMods];
    deleting B or A leaves C/SourceMods dangling, even though realpath() only shows A."""
    hops = []
    try:
        with os.scandir(str(case)) as it:
            links = [e.path for e in it if e.is_symlink()]
    except OSError:
        return hops
    for cur in links:
        for _ in range(40):
            if not os.path.islink(cur):
                break
            try:
                nxt = os.readlink(cur)
            except OSError:
                break
            if not os.path.isabs(nxt):
                nxt = os.path.join(os.path.dirname(cur), nxt)
            nxt = os.path.abspath(nxt)
            hop = Path(os.path.realpath(os.path.dirname(nxt))) / os.path.basename(nxt)
            hops.append(hop)
            cur = str(hop)
    return hops


def inventory(root):
    files = dirs = links = nbytes = 0
    for folder, dnames, fnames in os.walk(str(root), followlinks=False):
        dirs += 1
        for x in fnames + dnames:
            p = os.path.join(folder, x)
            st = os.lstat(p)
            if os.path.islink(p):
                links += 1
            elif x in fnames:
                files += 1
                nbytes += st.st_size
    return {'files': files, 'dirs': dirs, 'links': links, 'bytes': nbytes}


def build_plan(args):
    root = args.case_root.resolve(strict=True)
    scratch_roots = [Path(p).resolve() for p in args.scratch_root if Path(p).exists()]
    require(scratch_roots, 'No Scratch root exists: ' + ', '.join(args.scratch_root))
    paths, _ = A.discover(root)
    jobs = A.read_queue()
    names = [p.name for p in paths]
    related, unknown = A.relate_jobs(jobs, names)
    wanted = set(args.sites) - set(args.exclude_site)

    info = {}
    for p in paths:
        env, out, roots = case_outputs(p)
        info[p.name] = {'path': p, 'site': site_of(p.name), 'env': env, 'out': out, 'roots': roots}

    # Site-level holds: current experiments and anything live.
    site_hold = {}
    for name, d in info.items():
        if d['site'] not in wanted:
            continue
        cohort, note = A.classify(name)
        state, g = A.evaluate(d['path'], cohort, note, related[name], args.quiet_minutes, True)
        d['state'] = state['state']
        d['cohort'] = cohort
        d['years'] = state['years']
        if TRIAL_RE.search(name):
            site_hold.setdefault(d['site'], []).append('trial case ' + name)
        elif d['state'] in NOT_IDLE:
            site_hold.setdefault(d['site'], []).append('{} is {}'.format(name, d['state']))
    for site in wanted & PROTECTED_SITES:
        if site not in args.sites_explicit:
            site_hold.setdefault(site, []).append('protected example site')

    candidates, held, ignored = [], [], []
    for name in sorted(info):
        d = info[name]
        if d['site'] not in wanted:
            continue
        if d['cohort'] not in A.SCOPED and d['cohort'] != 'PROTECTED_EXAMPLE':
            ignored.append(name)   # legacy SPINUP / unrelated experiments: out of scope
            continue
        if d['site'] in site_hold:
            held.append((name, 'SITE HELD: ' + '; '.join(site_hold[d['site']][:3])))
            continue
        candidates.append(name)

    # Path checks and dependency scan against every case that stays.
    selected = set(candidates)
    others = [n for n in info if n not in selected]
    other_text = {n: dependency_text(info[n]['path']) for n in others}
    other_links = {n: rundir_symlink_targets(info[n]['out']['RUNDIR'])
                   for n in others if info[n]['out'].get('RUNDIR')}
    other_hops = {n: case_symlink_hops(info[n]['path']) for n in others}
    all_case_dirs = {n: info[n]['path'] for n in info}
    plan_cases = []
    for name in candidates:
        d = info[name]
        reasons = []
        roots = []
        for raw in d['roots']:
            if not raw.exists():
                continue
            if raw.is_symlink():
                reasons.append('output path is a symlink: ' + str(raw))
                continue
            r = raw.resolve()
            if not any(is_within(r, s) and r != s for s in scratch_roots):
                reasons.append('output path not under Scratch: ' + str(r))
                continue
            if name not in r.parts:
                reasons.append('output path does not name the case: ' + str(r))
                continue
            for other, cdir in all_case_dirs.items():
                if is_within(cdir, r) or is_within(r, cdir):
                    reasons.append('output path overlaps case dir {}: {}'.format(other, r))
            for other in info:
                if other == name:
                    continue
                for op in info[other]['roots']:
                    op = Path(os.path.realpath(str(op)))
                    if is_within(op, r) or is_within(r, op):
                        reasons.append('output path overlaps outputs of {}: {}'.format(other, r))
            roots.append(r)
        pat = A.token_re(name)
        root_strs = [str(r) for r in roots]
        for other in others:
            text = other_text[other]
            if pat.search(text) or any(s in text for s in root_strs):
                reasons.append('referenced by remaining case {} (env/user_nl/CaseDocs)'.format(other))
            for t in other_links.get(other, []):
                if any(is_within(t, r) for r in roots):
                    reasons.append('RUNDIR symlink of {} points into {}'.format(other, t))
                    break
            for h in other_hops.get(other, []):
                if any(is_within(h, r) for r in roots):
                    reasons.append('case-folder symlink of {} passes through {}'.format(other, h))
                    break
        if reasons:
            held.append((name, 'DEPENDENCY/PATH: ' + '; '.join(sorted(set(reasons))[:4])))
            continue
        if not roots:
            held.append((name, 'no Scratch outputs found (already offloaded?)'))
            continue
        exe = d['out'].get('EXEROOT')
        shared_exe = str(exe) if exe and not any(is_within(Path(os.path.realpath(str(exe))), r) for r in roots) else ''
        entry = {'case': name, 'site': d['site'], 'shared_exeroot_kept': shared_exe, 'cohort': d['cohort'], 'state': d['state'],
                 'years': d['years'], 'case_dir': str(d['path']),
                 'case_dir_inventory': inventory(d['path']),
                 'delete_roots': [{'path': str(r), **inventory(r)} for r in roots]}
        plan_cases.append(entry)

    # A case that a selected case depends on must not be deleted before it: treat
    # mutual references inside the selection as fine (they leave together).
    return {
        'version': VERSION, 'created_utc': now_utc(), 'host': socket.gethostname(),
        'case_root': str(root), 'scratch_roots': [str(s) for s in scratch_roots],
        'sites_requested': sorted(wanted), 'site_holds': {k: v for k, v in sorted(site_hold.items())},
        'unattributed_jobs': unknown, 'cases': plan_cases,
        'held': [{'case': n, 'reason': r} for n, r in held],
        'ignored_not_fixnc': ignored,
    }


def print_plan(plan):
    gb = 1024 ** 3
    print('OFFLOAD PLAN {}  host={}  UTC={}'.format(plan['version'], plan['host'], plan['created_utc']))
    print('case_root={}  sites_requested={}'.format(plan['case_root'], len(plan['sites_requested'])))
    for job in plan['unattributed_jobs']:
        print('  UNATTRIBUTED_JOB {id} {name} {state} WorkDir={workdir}'.format(**job))
    print('\nSITES HELD (current experiment / live / protected):')
    for site, why in plan['site_holds'].items():
        print('  {:<8} {}'.format(site, '; '.join(why[:4]) + (' ...' if len(why) > 4 else '')))
    print('\nOFFLOAD\tsite\tstate\tyears\tcase\tdelete_files\tdelete_dirs\tGB\tpaths')
    tf = td = tb = 0
    for c in plan['cases']:
        f = sum(r['files'] + r['links'] for r in c['delete_roots'])
        dd = sum(r['dirs'] for r in c['delete_roots'])
        b = sum(r['bytes'] for r in c['delete_roots'])
        tf, td, tb = tf + f, td + dd, tb + b
        print('OFFLOAD\t{}\t{}\t{}\t{}\t{}\t{}\t{:.2f}\t{}'.format(
            c['site'], c['state'], c['years'], c['case'], f, dd, b / gb,
            ' '.join(r['path'] for r in c['delete_roots'])))
    print('\nHELD\tcase\treason')
    for h in plan['held']:
        print('HELD\t{}\t{}'.format(h['case'], h['reason']))
    print('\nTOTAL cases_to_offload={} scratch_inodes_freed~={} GB={:.1f} held_cases={}'.format(
        len(plan['cases']), tf + td, tb / gb, len(plan['held'])))
    shared = sorted({c['shared_exeroot_kept'] for c in plan['cases'] if c.get('shared_exeroot_kept')})
    for sx in shared:
        n = sum(1 for c in plan['cases'] if c.get('shared_exeroot_kept') == sx)
        print('SHARED_EXEROOT_KEPT {} (used by {} offloaded case(s); never deleted)'.format(sx, n))
    print('Not FIXNC first-run cases (legacy SPINUP etc.), left alone: {}'.format(len(plan['ignored_not_fixnc'])))
    print('Case directories are archived too but stay on Scratch.')


# --------------------------------------------------------------------------- Research mount
def mount_info(path):
    resolved = Path(path).resolve(strict=True)
    doc = json.loads(A.command(['findmnt', '--json', '--target', str(resolved),
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
    real = [r for r in rows if r['fstype'] != 'autofs'] or rows
    deepest = max(len(Path(r['target']).parts) for r in real)
    sel = [r for r in real if len(Path(r['target']).parts) == deepest]
    require(len({tuple(r.values()) for r in sel}) == 1, 'Ambiguous mount: ' + json.dumps(sel))
    return sel[0]


def check_dest(dest, scratch_roots, allow_non_nfs):
    require(dest.parent.is_dir(), 'Research Filestore parent unavailable on this node: ' + str(dest.parent))
    os.listdir(str(dest.parent))  # trigger automount
    eff = dest.parent.resolve()
    for s in scratch_roots:
        require(not is_within(eff, Path(s)), 'Destination is on Scratch: ' + str(eff))
    require(not is_within(eff, Path.home().resolve()), 'Destination is in Home: ' + str(eff))
    info = mount_info(dest.parent)
    print('DEST_MOUNT', json.dumps(dict(path=str(dest.parent), **info)), flush=True)
    if not allow_non_nfs:
        require(info['fstype'] in ('nfs', 'nfs4') and info['target'] != '/',
                'Destination is not a verified NFS Research mount; no archive/deletion')
    return info


# --------------------------------------------------------------------------- archive
class HashingReader:
    def __init__(self, handle):
        self.handle, self.h, self.n = handle, hashlib.sha256(), 0

    def read(self, size=-1):
        data = self.handle.read(size)
        self.h.update(data)
        self.n += len(data)
        return data


def walk_sorted(root):
    for folder, dnames, fnames in os.walk(str(root), followlinks=False):
        dnames.sort()
        yield folder, True
        for x in sorted(fnames):
            yield os.path.join(folder, x), False
        for x in dnames:
            p = os.path.join(folder, x)
            if os.path.islink(p):
                yield p, False


def sha256_file(path, bufsize=8 * 1024 * 1024):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(bufsize), b''):
            h.update(chunk)
    return h.hexdigest()


def write_case_tar(entry, session):
    """One tar per case: case dir (kept on Scratch) + delete roots. Returns manifest rows."""
    name = entry['case']
    partial = session / (name + '.tar.partial')
    rows = []
    sources = [('keep', Path(entry['case_dir']))] + [('delete', Path(r['path'])) for r in entry['delete_roots']]
    with tarfile.open(str(partial), 'w', format=tarfile.PAX_FORMAT, dereference=False) as tar:
        for role, src in sources:
            for path, is_dir in walk_sorted(src):
                arc = path.lstrip('/')
                st = os.lstat(path)
                ti = tar.gettarinfo(path, arcname=arc)
                if ti.isreg():
                    with open(path, 'rb') as handle:
                        reader = HashingReader(handle)
                        tar.addfile(ti, reader)
                    st2 = os.lstat(path)
                    require(st2.st_size == st.st_size and st2.st_mtime_ns == st.st_mtime_ns and reader.n == st.st_size,
                            'File changed while archiving: ' + path)
                    rows.append((role, 'file', path, st.st_size, st.st_mtime_ns, reader.h.hexdigest()))
                elif ti.issym():
                    tar.addfile(ti)
                    rows.append((role, 'symlink', path, 0, st.st_mtime_ns, ti.linkname))
                elif ti.islnk():
                    tar.addfile(ti)
                    rows.append((role, 'hardlink', path, st.st_size, st.st_mtime_ns, '/' + ti.linkname))
                elif ti.isdir():
                    tar.addfile(ti)
                    rows.append((role, 'dir', path, 0, st.st_mtime_ns, '-'))
                else:
                    raise Stop('Special file needs review (not archived): ' + path)
    with open(str(partial), 'rb') as f:
        os.fsync(f.fileno())
        if hasattr(os, 'posix_fadvise'):
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    final = session / (name + '.tar')
    os.rename(str(partial), str(final))
    manifest = session / (name + '.manifest.tsv')
    with manifest.open('w') as f:
        f.write('role\tkind\tpath\tsize\tmtime_ns\tsha256_or_target\n')
        for r in rows:
            f.write('\t'.join(str(x) for x in r) + '\n')
    return final, manifest, rows


def read_manifest(path):
    rows = []
    with Path(path).open() as f:
        next(f)
        for line in f:
            role, kind, p, size, mtime, extra = line.rstrip('\n').split('\t')
            rows.append((role, kind, p, int(size), int(mtime), extra))
    return rows


def verify_tar(tar_path, rows):
    """Re-read the tar from the destination and compare every member with the manifest."""
    expect = {r[2].lstrip('/'): r for r in rows}
    seen = set()
    with tarfile.open(str(tar_path), 'r:') as tar:
        for m in tar:
            r = expect.get(m.name.rstrip('/'))
            require(r is not None, 'Unexpected tar member: ' + m.name)
            seen.add(m.name.rstrip('/'))
            kind = r[1]
            if kind == 'file':
                require(m.isreg() and m.size == r[3], 'Size/type mismatch: ' + m.name)
                h = hashlib.sha256()
                f = tar.extractfile(m)
                for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
                    h.update(chunk)
                require(h.hexdigest() == r[5], 'SHA256 mismatch: ' + m.name)
            elif kind == 'symlink':
                require(m.issym() and m.linkname == r[5], 'Symlink mismatch: ' + m.name)
            elif kind == 'hardlink':
                require(m.islnk() and '/' + m.linkname == r[5], 'Hardlink mismatch: ' + m.name)
            elif kind == 'dir':
                require(m.isdir(), 'Dir mismatch: ' + m.name)
    require(seen == set(expect), 'Missing tar members: {}'.format(len(set(expect) - seen)))
    return len(seen)


def recheck_case(entry, plan, args):
    """Fresh check right before archive/clean: still idle, same paths, no new dependants."""
    root = Path(plan['case_root'])
    name = entry['case']
    case = root / name
    require(case.is_dir(), 'case dir missing: ' + str(case))
    jobs = A.read_queue()
    paths, _ = A.discover(root)
    names = [p.name for p in paths]
    related, _ = A.relate_jobs(jobs, names)
    cohort, note = A.classify(name)
    state, g = A.evaluate(case, cohort, note, related[name], args.quiet_minutes, True)
    require(state['state'] not in NOT_IDLE, 'case is now ' + state['state'])
    site = site_of(name)
    for p in paths:
        if site_of(p.name) == site and p.name != name and TRIAL_RE.search(p.name):
            raise Stop('site now has trial case ' + p.name)
        if site_of(p.name) == site and related[p.name]:
            raise Stop('site has live job(s) for ' + p.name)
    _, _, roots = case_outputs(case)
    planned = {r['path'] for r in entry['delete_roots']}
    require({os.path.realpath(str(r)) for r in roots if r.exists()} <= planned,
            'output paths changed since plan')
    selected = {c['case'] for c in plan['cases']}
    pat = A.token_re(name)
    for p in paths:
        if p.name in selected:
            continue
        text = dependency_text(p)
        if pat.search(text) or any(s in text for s in planned):
            raise Stop('now referenced by remaining case ' + p.name)
        _, out, _ = case_outputs(p)
        if out.get('RUNDIR'):
            for t in rundir_symlink_targets(out['RUNDIR']):
                if any(is_within(t, Path(s)) for s in planned):
                    raise Stop('RUNDIR symlink of {} points into {}'.format(p.name, t))
        for h in case_symlink_hops(p):
            if any(is_within(h, Path(s)) for s in planned):
                raise Stop('case-folder symlink of {} passes through {}'.format(p.name, h))


def load_plan(path):
    plan = json.loads(Path(path).read_text())
    require(plan.get('version') == VERSION, 'Plan was made by another version: ' + str(plan.get('version')))
    return plan


def do_archive(args):
    plan = load_plan(args.plan)
    cases = [c for c in plan['cases'] if not args.case or c['case'] in args.case]
    require(cases, 'No cases selected from plan')
    check_dest(args.dest, plan['scratch_roots'], args.allow_non_nfs_dest)
    args.dest.mkdir(exist_ok=True)
    need = sum(r['bytes'] for c in cases for r in c['delete_roots']) + \
        sum(c['case_dir_inventory']['bytes'] for c in cases)
    st = os.statvfs(str(args.dest))
    free = st.f_bavail * st.f_frsize
    print('DEST_FREE_GB={:.1f} NEED_GB~={:.1f}'.format(free / 1024 ** 3, need / 1024 ** 3), flush=True)
    require(free > need * 1.05 + 2 * 1024 ** 3, 'Not enough space on the Research destination')
    session = args.dest / ('offload_' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '_' + uuid.uuid4().hex[:8])
    session.mkdir()
    (session / 'plan.json').write_text(json.dumps(plan, indent=1))
    print('SESSION', session, flush=True)
    ok, failed = [], []
    for entry in cases:
        name = entry['case']
        t0 = time.time()
        try:
            recheck_case(entry, plan, args)
            tar_path, manifest, rows = write_case_tar(entry, session)
            n = verify_tar(tar_path, rows)
            digest = sha256_file(str(tar_path))
            receipt = {'case': name, 'tar': tar_path.name, 'tar_sha256': digest,
                       'tar_size': tar_path.stat().st_size, 'members': n,
                       'verified_utc': now_utc(), 'host': socket.gethostname(), 'version': VERSION}
            (session / (name + '.VERIFIED')).write_text(json.dumps(receipt, indent=1))
            print('ARCHIVED_VERIFIED\t{}\tmembers={}\tGB={:.2f}\tseconds={:.0f}'.format(
                name, n, receipt['tar_size'] / 1024 ** 3, time.time() - t0), flush=True)
            ok.append(name)
        except (Stop, OSError, tarfile.TarError) as exc:
            print('ARCHIVE_SKIPPED\t{}\t{}'.format(name, exc), flush=True)
            for leftover in (session / (name + '.tar.partial'),):
                if leftover.exists():
                    leftover.unlink()
            failed.append(name)
    print('\nARCHIVE_DONE session={} verified={} skipped={}'.format(session, len(ok), len(failed)))
    print('Nothing was deleted. Next: clean --session {} --confirm {}'.format(session, TOKEN))
    return 0 if ok else 1


def do_verify(args):
    session = args.session
    receipts = sorted(session.glob('*.VERIFIED'))
    require(receipts, 'No receipts in ' + str(session))
    bad = 0
    for rec in receipts:
        r = json.loads(rec.read_text())
        try:
            rows = read_manifest(session / (r['case'] + '.manifest.tsv'))
            tar_path = session / r['tar']
            require(sha256_file(str(tar_path)) == r['tar_sha256'], 'tar SHA256 differs from receipt')
            n = verify_tar(tar_path, rows)
            print('VERIFIED\t{}\tmembers={}'.format(r['case'], n), flush=True)
        except (Stop, OSError, tarfile.TarError) as exc:
            bad += 1
            print('VERIFY_FAILED\t{}\t{}'.format(r['case'], exc), flush=True)
    print('VERIFY_SESSION {} ok={} failed={}'.format(session, len(receipts) - bad, bad))
    return 1 if bad else 0


# --------------------------------------------------------------------------- clean
def do_clean(args):
    require(args.confirm == TOKEN, 'clean requires --confirm ' + TOKEN)
    session = args.session
    plan = load_plan(session / 'plan.json')
    check_dest(session, plan['scratch_roots'], args.allow_non_nfs_dest)
    log = (session / 'cleanup_actions.tsv').open('a')
    total_files = total_dirs = kept = 0
    for entry in plan['cases']:
        name = entry['case']
        if args.case and name not in args.case:
            continue
        rec = session / (name + '.VERIFIED')
        if not rec.exists():
            print('CLEAN_SKIPPED\t{}\tno VERIFIED receipt'.format(name))
            continue
        r = json.loads(rec.read_text())
        tar_path = session / r['tar']
        try:
            require(tar_path.stat().st_size == r['tar_size'], 'tar size differs from receipt')
            if args.rehash:
                require(sha256_file(str(tar_path)) == r['tar_sha256'], 'tar SHA256 differs from receipt')
            recheck_case(entry, plan, args)
        except (Stop, OSError) as exc:
            print('CLEAN_SKIPPED\t{}\t{}'.format(name, exc), flush=True)
            continue
        rows = [x for x in read_manifest(session / (name + '.manifest.tsv')) if x[0] == 'delete']
        roots = [Path(d['path']) for d in entry['delete_roots']]
        files = dirs = changed = 0
        for role, kind, p, size, mtime, extra in rows:
            path = Path(p)
            require(any(is_within(path, rt) for rt in roots), 'manifest path outside delete roots: ' + p)
            if kind == 'dir':
                continue
            try:
                st = os.lstat(p)
            except FileNotFoundError:
                continue
            same = False
            if kind in ('file', 'hardlink') and not os.path.islink(p):
                same = st.st_size == size and st.st_mtime_ns == mtime
            elif kind == 'symlink' and os.path.islink(p):
                same = os.readlink(p) == extra
            if same:
                os.unlink(p)
                files += 1
                log.write('{}\tunlink\t{}\t{}\n'.format(now_utc(), name, p))
            else:
                changed += 1
                log.write('{}\tKEPT_CHANGED\t{}\t{}\n'.format(now_utc(), name, p))
        # Remove now-empty directories bottom-up; anything not in the manifest keeps its dir.
        for rt in roots:
            if not rt.exists():
                continue
            for folder, dnames, fnames in os.walk(str(rt), topdown=False, followlinks=False):
                try:
                    os.rmdir(folder)
                    dirs += 1
                    log.write('{}\trmdir\t{}\t{}\n'.format(now_utc(), name, folder))
                except OSError:
                    pass
            parent = rt.parent
            if parent.name == name and parent.exists():
                try:
                    os.rmdir(str(parent))
                    dirs += 1
                except OSError:
                    pass
        log.flush()
        leftover = sum(1 for rt in roots if rt.exists())
        print('CLEANED\t{}\tunlinked={}\trmdir={}\tkept_changed={}\troots_left={}'.format(
            name, files, dirs, changed, leftover), flush=True)
        total_files, total_dirs, kept = total_files + files, total_dirs + dirs, kept + changed
    log.close()
    print('\nCLEAN_DONE unlinked={} rmdir={} kept_changed={} log={}'.format(
        total_files, total_dirs, kept, session / 'cleanup_actions.tsv'))
    print('Case directories were NOT removed. Run myfiles for the real quota.')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--version', action='version', version=VERSION)
    p.add_argument('mode', choices=['plan', 'probe-mount', 'archive', 'verify', 'clean'])
    p.add_argument('--case-root', type=Path, default=A.DEFAULT_ROOT)
    p.add_argument('--scratch-root', action='append', default=None)
    p.add_argument('--sites', nargs='+', help='Replace the default failed-site list')
    p.add_argument('--exclude-site', nargs='+', default=[])
    p.add_argument('--plan', type=Path, help='archive: plan JSON written by `plan`')
    p.add_argument('--session', type=Path, help='verify/clean: Research session directory')
    p.add_argument('--case', action='append', help='archive/clean only these case(s) from the plan')
    p.add_argument('--dest', type=Path, default=DEFAULT_DEST)
    p.add_argument('--quiet-minutes', type=float, default=120)
    p.add_argument('--confirm', default='')
    p.add_argument('--rehash', action='store_true', help='clean: re-hash each tar before deleting (slower)')
    p.add_argument('--allow-non-nfs-dest', action='store_true', help=argparse.SUPPRESS)  # tests only
    args = p.parse_args(argv)
    args.scratch_root = args.scratch_root or list(DEFAULT_SCRATCH_ROOTS)
    args.sites_explicit = set(args.sites or [])
    args.sites = args.sites or FAILED_SITES
    try:
        if args.mode == 'plan':
            plan = build_plan(args)
            print_plan(plan)
            PLAN_DIR.mkdir(exist_ok=True)
            out = PLAN_DIR / ('offload_plan_' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '.json')
            out.write_text(json.dumps(plan, indent=1))
            print('PLAN_WRITTEN', out)
            print('READ ONLY: nothing archived or deleted.')
            return 0
        if args.mode == 'probe-mount':
            check_dest(args.dest, args.scratch_root, True)
            return 0
        if args.mode == 'archive':
            require(args.plan, 'archive needs --plan')
            return do_archive(args)
        require(args.session, args.mode + ' needs --session')
        args.session = args.session.resolve(strict=True)
        return do_verify(args) if args.mode == 'verify' else do_clean(args)
    except (Stop, OSError, RuntimeError) as exc:
        print('STOP: {}'.format(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
