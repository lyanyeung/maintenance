#!/usr/bin/env python3
"""Move the legacy 6/8/11/13-site 1000-year Normal spin-up cases off Scratch.

Scope (CLM-FATES-single-site-workflows/examples/FLUXNET_MultiSite_6, _8, _11, _13):
every case at the 38 legacy sites whose name is
    <SITE>_FATES_SPINUP500[...]   <SITE>_FATES_SPINUP1000[...]   IT-Noe_ERA5_ABW_BRANCH<yyyy>
(e.g. *_FATES_SPINUP500, *_SPINUP500_11P, *_SPINUP1000_S30SHARED, *_S30EXCL, CH-Aws RH* trials).
FIXNC (AD400/NORMAL400, DIAG/FIX/PHENFIX) cases and protected examples are never selected.

What happens to each selected case
  * first 1000 model years -> one tar on the Research Filestore, SHA256 per file, read back
    and compared before anything is deleted: case dir, bld (case-owned only), run dir and the
    short-term archive (histories year <= start+999, restarts year <= start+1000, logs);
  * output BEYOND 1000 years (the FLUXNET_MultiSite_6 1000->3000 continuation of RU-Fyo,
    DE-Tha, DK-Sor, NL-Loo, IL-Yat): histories year > start+999, restarts year > start+1000 and
    the run dir of a case that ran past 1000 years are NOT copied, only deleted
    (use --copy-beyond-1000 at plan time to copy them too);
  * clean then deletes everything listed in the manifest (case dir included unless
    --keep-case-dirs) whose size and mtime are unchanged since the archive.

Held (not touched): a case with a live/recent Slurm job, a Git lock, recent writes or no
CaseStatus; any case that a remaining case still references (env_*.xml, user_nl_*, CaseDocs,
RUNDIR symlinks), and any case that a held case references (repeated until stable);
a shared EXEROOT whose path does not name the case.

Modes (one step at a time, nothing is deleted before `clean`):
  plan         READ ONLY. Writes ~/offload_plans/legacy1000_plan_<UTC>.json
  probe-mount  READ ONLY. Shows the effective filesystem of the Research destination.
  archive      --plan P. Writes <dest>/legacy1000_<UTC>_<id>/<case>.tar + manifest + VERIFIED.
  verify       --session S. Re-reads every tar against its manifest (read only).
  clean        --session S --confirm DELETE_VERIFIED_LEGACY_SPINUP1000

Needs offload_failed_cases.py and audit_pack_fixnc_case_git.py in the same folder.
Python 3.9+, no third-party modules.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import sys
import tarfile
import time
import uuid

VERSION = '20261102-legacy1000-v1'
TOKEN = 'DELETE_VERIFIED_LEGACY_SPINUP1000'
DEFAULT_DEST = Path('/research/xclimatetbm/ly3n24/Scratch_Offload')
DEFAULT_OUT_ROOT = Path('/scratch/ly3n24/CTSM_FATES_OUTPUTS')
PLAN_DIR = Path.home() / 'offload_plans'
HERE = Path(__file__).resolve().parent


def _load(modname, filename):
    spec = importlib.util.spec_from_file_location(modname, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


A = _load('fixnc_audit', 'audit_pack_fixnc_case_git.py')
O = _load('offload_failed', 'offload_failed_cases.py')
Stop, require, now_utc, is_within = O.Stop, O.require, O.now_utc, O.is_within

# RUN_START_YEAR per site (maintenance/check_four_spinups.py in CLM-FATES-single-site-workflows).
BATCH_START = {
    '6': {'DE-Hai': 2000, 'RU-Fyo': 1998, 'DE-Tha': 1996, 'DK-Sor': 1996, 'NL-Loo': 1997, 'IL-Yat': 2000},
    '8': {'CZ-BK1': 2004, 'IT-Ren': 1999, 'FR-Hes': 1997, 'FR-FBn': 2008, 'DE-Hzd': 2010,
          'CH-Dav': 1997, 'BE-Vie': 1996, 'IT-Lav': 2003},
    '11': {'BE-Bra': 1996, 'FR-Fon': 2005, 'GL-ZaH': 2000, 'IT-Noe': 2004, 'AT-Neu': 2002,
           'FI-Sod': 2001, 'CH-Aws': 2006, 'CH-Cha': 2005, 'CH-Fru': 2005, 'CH-Lae': 2004,
           'CZ-Stn': 2010},
    '13': {'DE-Lnf': 2002, 'DE-Obe': 2008, 'ES-Agu': 2006, 'ES-LJu': 2004, 'IT-Col': 1996,
           'DE-Gri': 2004, 'DE-RuR': 2011, 'FI-Hyy': 1997, 'FI-Let': 2010, 'FR-Pue': 2000,
           'GF-Guy': 2004, 'IT-MBo': 2003, 'IT-Tor': 2008},
}
START = {s: y for b in BATCH_START.values() for s, y in b.items()}
BATCH_OF = {s: b for b, sites in BATCH_START.items() for s in sites}
FAMILY_RE = re.compile(r'^(?P<site>[A-Z]{2}-[A-Za-z0-9]{2,3})_'
                       r'(?:FATES_SPINUP(?:500|1000)(?:_[A-Za-z0-9]+)*|ERA5_ABW_BRANCH\d{4})$')
SPINUP_LIKE_RE = re.compile(r'SPINUP|BRANCH', re.I)
HIST_YEAR_RE = re.compile(r'\.(\d{4,})-\d\d(?:-\d\d(?:-\d{5})?)?\.nc(?:\.gz)?$')
H0A_RE = re.compile(r'\.clm2\.h0a\.(\d{4,})-')
NOT_IDLE = O.NOT_IDLE


def in_family(name):
    m = FAMILY_RE.match(name)
    if not m or m.group('site') not in START:
        return False
    cohort, _ = A.classify(name)
    return cohort not in A.SCOPED and cohort != 'PROTECTED_EXAMPLE'


def env_start_year(env):
    s = env.get('RUN_STARTDATE', '')
    return int(s[:4]) if re.match(r'^\d{4}', s) else None


def first1000_status(dout, start):
    hist = Path(dout) / 'lnd' / 'hist' if dout else None
    years = set()
    if hist and hist.is_dir():
        for name in os.listdir(str(hist)):
            m = H0A_RE.search(name)
            if m and name.endswith('.nc'):
                years.add(int(m.group(1)))
    n = len([y for y in years if start <= y <= start + 999])
    return n, (max(years) if years else None)


# --------------------------------------------------------------------------- split rule
def make_rule(entry):
    """Return f(path) -> 'copy' | 'nocopy'. Only cases with discard_beyond=True ever say nocopy."""
    if not entry['discard_beyond']:
        return lambda path: 'copy'
    start = entry['start']
    run = Path(entry['rundir']) if entry.get('rundir') else None
    dout = Path(entry['dout']) if entry.get('dout') else None
    run_beyond = entry['last_rest_year'] is not None and entry['last_rest_year'] > start + 1000

    def rule(path):
        p = Path(path)
        if run is not None and run_beyond and is_within(p, run):
            return 'nocopy'
        if dout is not None and is_within(p, dout) and p != dout:
            rel = p.relative_to(dout).parts
            if rel[0] == 'rest' and len(rel) > 1:
                m = A.REST_RE.match(rel[1])
                if m:
                    return 'nocopy' if int(m.group(1)) > start + 1000 else 'copy'
            m = HIST_YEAR_RE.search(p.name)
            if m and rel[0] != 'logs':
                return 'nocopy' if int(m.group(1)) > start + 999 else 'copy'
        return 'copy'
    return rule


def split_inventory(root, rule):
    out = {'copy': [0, 0], 'nocopy': [0, 0]}   # [entries, bytes]
    for folder, dnames, fnames in os.walk(str(root), followlinks=False):
        for x in fnames + [d for d in dnames if os.path.islink(os.path.join(folder, d))]:
            p = os.path.join(folder, x)
            st = os.lstat(p)
            k = rule(p)
            out[k][0] += 1
            out[k][1] += 0 if os.path.islink(p) else st.st_size
    return out


# --------------------------------------------------------------------------- plan
def case_roots(case, keep_case_dirs):
    env, paths, roots = O.case_outputs(case)
    return env, paths, roots, ([] if keep_case_dirs else [case])


def check_root(name, raw, scratch_roots, all_case_dirs, info, own_case_dir):
    reasons = []
    if raw.is_symlink():
        return None, ['path is a symlink: ' + str(raw)]
    r = raw.resolve()
    if not any(is_within(r, s) and r != s for s in scratch_roots):
        return None, ['path not under Scratch: ' + str(r)]
    if name not in r.parts:
        return None, ['path does not name the case: ' + str(r)]
    for other, cdir in all_case_dirs.items():
        if own_case_dir and other == name:
            continue
        if is_within(cdir, r) or is_within(r, cdir):
            reasons.append('overlaps case dir {}: {}'.format(other, r))
    for other, d in info.items():
        if other == name:
            continue
        for op in d['roots']:
            op = Path(os.path.realpath(str(op)))
            if is_within(op, r) or is_within(r, op):
                reasons.append('overlaps outputs of {}: {}'.format(other, r))
    return r, reasons


def build_plan(args):
    root = args.case_root.resolve(strict=True)
    scratch_roots = [Path(p).resolve() for p in args.scratch_root if Path(p).exists()]
    require(scratch_roots, 'No Scratch root exists: ' + ', '.join(args.scratch_root))
    paths, _ = A.discover(root)
    jobs = A.read_queue()                      # fails closed if squeue fails
    names = [p.name for p in paths]
    related, unknown = A.relate_jobs(jobs, names)

    info = {}
    for p in paths:
        env, out, roots = O.case_outputs(p)
        info[p.name] = {'path': p, 'env': env, 'out': out, 'roots': roots}
    all_case_dirs = {n: info[n]['path'] for n in info}

    family = [n for n in names if in_family(n)]
    if args.sites:
        family = [n for n in family if FAMILY_RE.match(n).group('site') in args.sites]
    family = [n for n in family if n not in set(args.exclude_case)]
    extra = [n for n in args.extra_case if n in info and n not in family]
    missing_extra = [n for n in args.extra_case if n not in info]
    require(not missing_extra, 'Unknown --extra-case: ' + ', '.join(missing_extra))
    selected = family + extra
    not_selected_spinup = sorted(n for n in names if n not in selected and SPINUP_LIKE_RE.search(n)
                                 and A.classify(n)[0] not in A.SCOPED)

    held = {}
    entries = {}
    for name in selected:
        d = info[name]
        m = FAMILY_RE.match(name)
        site = m.group('site') if m else (A.NAME_RE.match(name).group('site') if A.NAME_RE.match(name) else '?')
        state, _ = A.evaluate(d['path'], 'LEGACY', '', related[name], args.quiet_minutes, True)
        reasons = []
        if state['state'] in NOT_IDLE:
            reasons.append('state ' + state['state'] + (' jobs=' + state['jobs'] if state['jobs'] != '-' else ''))
        roots, own = [], []
        for raw in d['roots']:
            if raw.exists():
                r, why = check_root(name, raw, scratch_roots, all_case_dirs, info, False)
                reasons += why
                if r is not None and not why:
                    roots.append(r)
        if not args.keep_case_dirs:
            r, why = check_root(name, d['path'], scratch_roots, all_case_dirs, info, True)
            reasons += why
            if r is not None and not why:
                own.append(r)
        start = START.get(site)
        env_start = env_start_year(d['env'])
        dout = d['out'].get('DOUT_S_ROOT')
        n1000, max_h0a = first1000_status(str(dout) if dout else None, start) if start else (0, None)
        last_rest = A.rest_years(str(dout)) if dout else None
        beyond = start is not None and ((last_rest is not None and last_rest > start + 1000)
                                        or (max_h0a is not None and max_h0a > start + 999))
        note = []
        if start is None:
            note.append('site start year unknown')
        elif env_start != start:
            note.append('RUN_STARTDATE {} != table {}'.format(env_start, start))
        discard = bool(beyond and not args.copy_beyond_1000 and env_start == start)
        if beyond and not discard and not args.copy_beyond_1000:
            note.append('beyond-1000 output will be COPIED (start year not confirmed)')
        exe = d['out'].get('EXEROOT')
        shared_exe = str(exe) if exe and not any(is_within(Path(os.path.realpath(str(exe))), r) for r in roots) else ''
        entries[name] = {
            'case': name, 'site': site, 'batch': BATCH_OF.get(site, '?'), 'state': state['state'],
            'start': start, 'env_start': env_start, 'last_rest_year': last_rest,
            'h0a_first1000': n1000, 'complete_1000': n1000 == 1000, 'beyond_1000': beyond,
            'discard_beyond': discard, 'note': '; '.join(note),
            'case_dir': str(d['path']), 'keep_case_dir': bool(args.keep_case_dirs),
            'rundir': str(d['out']['RUNDIR']) if d['out'].get('RUNDIR') else '',
            'dout': str(dout) if dout else '', 'shared_exeroot_kept': shared_exe,
            'delete_roots': [str(r) for r in roots] + [str(r) for r in own],
        }
        if reasons:
            held[name] = 'PATH/STATE: ' + '; '.join(sorted(set(reasons))[:4])
        elif not roots and args.keep_case_dirs:
            held[name] = 'no Scratch outputs found (already offloaded?)'

    # Dependency closure: a case referenced by any case that stays (unselected or held) stays.
    texts = {n: O.dependency_text(info[n]['path']) for n in names}
    links = {n: O.rundir_symlink_targets(info[n]['out']['RUNDIR'])
             for n in names if info[n]['out'].get('RUNDIR')}
    changed = True
    while changed:
        changed = False
        staying = [n for n in names if n not in entries or n in held]
        for name, e in entries.items():
            if name in held:
                continue
            pat = A.token_re(name)
            rs = [str(info[name]['path'])] + [str(Path(os.path.realpath(str(r)))) for r in info[name]['roots']]
            for other in staying:
                if other == name:
                    continue
                if pat.search(texts.get(other, '')) or any(s in texts.get(other, '') for s in rs):
                    held[name] = 'DEPENDENCY: referenced by remaining case ' + other
                elif any(any(is_within(t, Path(s)) for s in rs) for t in links.get(other, [])):
                    held[name] = 'DEPENDENCY: RUNDIR symlink of {} points into it'.format(other)
                if name in held:
                    changed = True
                    break

    # Orphan output dirs (case dir gone): reported only.
    covered = {Path(os.path.realpath(str(r))) for d in info.values() for r in d['roots']}
    orphans = []
    out_root = args.out_root
    for base in (out_root, out_root / 'archive'):
        try:
            for x in sorted(os.listdir(str(base))):
                p = Path(os.path.realpath(str(base / x)))
                site = x.split('_')[0]
                if site in START and SPINUP_LIKE_RE.search(x) and x not in info and p not in covered:
                    orphans.append(str(base / x))
        except OSError:
            pass

    plan_cases = []
    for name in selected:
        e = entries[name]
        if name in held:
            continue
        rule = make_rule(e)
        inv = []
        for r in e['delete_roots']:
            sp = split_inventory(r, rule)
            inv.append({'path': r, 'copy_entries': sp['copy'][0], 'copy_bytes': sp['copy'][1],
                        'nocopy_entries': sp['nocopy'][0], 'nocopy_bytes': sp['nocopy'][1],
                        'dirs': sum(1 for _ in os.walk(r))})
        if e['keep_case_dir']:
            sp = split_inventory(e['case_dir'], lambda p: 'copy')
            e['case_dir_inventory'] = {'copy_entries': sp['copy'][0], 'copy_bytes': sp['copy'][1]}
        e['roots_inventory'] = inv
        plan_cases.append(e)

    return {
        'version': VERSION, 'created_utc': now_utc(), 'host': socket.gethostname(),
        'case_root': str(root), 'scratch_roots': [str(s) for s in scratch_roots],
        'copy_beyond_1000': bool(args.copy_beyond_1000), 'keep_case_dirs': bool(args.keep_case_dirs),
        'unattributed_jobs': unknown, 'cases': plan_cases,
        'held': [{'case': n, 'reason': held[n], 'state': entries[n]['state']} for n in selected if n in held],
        'not_selected_spinup_like': not_selected_spinup, 'orphan_output_dirs': orphans,
    }


def print_plan(plan):
    gb = 1024 ** 3
    print('LEGACY SPINUP1000 OFFLOAD PLAN {}  host={}  UTC={}'.format(plan['version'], plan['host'], plan['created_utc']))
    print('case_root={}  copy_beyond_1000={}  keep_case_dirs={}'.format(
        plan['case_root'], plan['copy_beyond_1000'], plan['keep_case_dirs']))
    for job in plan['unattributed_jobs']:
        print('  UNATTRIBUTED_JOB {id} {name} {state} WorkDir={workdir}'.format(**job))
    print('\nOFFLOAD\tbatch\tsite\tstate\th0a_1..1000\tlast_rest\tcase\tcopy_GB\tdiscard_GB(>1000y)\tinodes_freed\tnote')
    tc = tn = ti = 0
    for c in plan['cases']:
        cb = sum(r['copy_bytes'] for r in c['roots_inventory'])
        nb = sum(r['nocopy_bytes'] for r in c['roots_inventory'])
        ino = sum(r['copy_entries'] + r['nocopy_entries'] + r['dirs'] for r in c['roots_inventory'])
        tc, tn, ti = tc + cb, tn + nb, ti + ino
        print('OFFLOAD\t{}\t{}\t{}\t{}/1000\t{}\t{}\t{:.2f}\t{:.2f}\t{}\t{}'.format(
            c['batch'], c['site'], c['state'], c['h0a_first1000'], c['last_rest_year'], c['case'],
            cb / gb, nb / gb, ino, (('INCOMPLETE; ' if not c['complete_1000'] else '')
                                    + ('BEYOND1000_DISCARDED; ' if c['discard_beyond'] else '') + c['note']) or '-'))
    print('\nHELD\tcase\tstate\treason')
    for h in plan['held']:
        print('HELD\t{}\t{}\t{}'.format(h['case'], h['state'], h['reason']))
    if plan['not_selected_spinup_like']:
        print('\nNOT_SELECTED (SPINUP-like name outside the legacy family, left alone; add with --extra-case):')
        for n in plan['not_selected_spinup_like']:
            print('  ' + n)
    if plan['orphan_output_dirs']:
        print('\nORPHAN_OUTPUT (no case dir; NOT handled by this tool, review by hand):')
        for p in plan['orphan_output_dirs']:
            print('  ' + p)
    shared = sorted({c['shared_exeroot_kept'] for c in plan['cases'] if c['shared_exeroot_kept']})
    for sx in shared:
        print('SHARED_EXEROOT_KEPT {} (never deleted)'.format(sx))
    print('\nTOTAL cases_to_offload={} copy_GB={:.1f} discard_GB={:.1f} scratch_inodes_freed~={} held={}'.format(
        len(plan['cases']), tc / gb, tn / gb, ti, len(plan['held'])))


# --------------------------------------------------------------------------- archive
def write_case_tar(entry, session):
    name = entry['case']
    rule = make_rule(entry)
    partial = session / (name + '.tar.partial')
    rows = []
    sources = [('delete', Path(r)) for r in entry['delete_roots']]
    if entry['keep_case_dir']:
        sources = [('keep', Path(entry['case_dir']))] + sources
    with tarfile.open(str(partial), 'w', format=tarfile.PAX_FORMAT, dereference=False) as tar:
        for role, src in sources:
            for path, is_dir in O.walk_sorted(src):
                st = os.lstat(path)
                if role == 'delete' and rule(path) == 'nocopy':
                    kind = 'dir' if is_dir else 'symlink' if os.path.islink(path) else 'file'
                    extra = os.readlink(path) if kind == 'symlink' else '-'
                    rows.append(('nocopy', kind, path, 0 if kind != 'file' else st.st_size, st.st_mtime_ns, extra))
                    continue
                ti = tar.gettarinfo(path, arcname=path.lstrip('/'))
                if ti.isreg():
                    with open(path, 'rb') as handle:
                        reader = O.HashingReader(handle)
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
    return final, rows


def recheck_case(entry, plan, args):
    """Fresh check right before archive/clean: idle, same paths, no new dependants."""
    root = Path(plan['case_root'])
    name = entry['case']
    case = root / name
    require(case.is_dir(), 'case dir missing: ' + str(case))
    paths, _ = A.discover(root)
    related, _ = A.relate_jobs(A.read_queue(), [p.name for p in paths])
    state, _ = A.evaluate(case, 'LEGACY', '', related[name], args.quiet_minutes, True)
    require(state['state'] not in NOT_IDLE, 'case is now ' + state['state'])
    _, _, roots = O.case_outputs(case)
    planned = set(entry['delete_roots'])
    require({os.path.realpath(str(r)) for r in roots if r.exists()} <= planned, 'output paths changed since plan')
    selected = {c['case'] for c in plan['cases']}
    pat = A.token_re(name)
    for p in paths:
        if p.name in selected:
            continue
        text = O.dependency_text(p)
        if pat.search(text) or any(s in text for s in planned if s != str(case)):
            raise Stop('now referenced by remaining case ' + p.name)
        _, out, _ = O.case_outputs(p)
        if out.get('RUNDIR'):
            for t in O.rundir_symlink_targets(out['RUNDIR']):
                if any(is_within(t, Path(s)) for s in planned):
                    raise Stop('RUNDIR symlink of {} points into {}'.format(p.name, t))


def load_plan(path):
    plan = json.loads(Path(path).read_text())
    require(plan.get('version') == VERSION, 'Plan was made by another version: ' + str(plan.get('version')))
    return plan


def do_archive(args):
    plan = load_plan(args.plan)
    cases = [c for c in plan['cases'] if not args.case or c['case'] in args.case]
    require(cases, 'No cases selected from plan')
    O.check_dest(args.dest, plan['scratch_roots'], args.allow_non_nfs_dest)
    args.dest.mkdir(exist_ok=True)
    need = sum(r['copy_bytes'] for c in cases for r in c['roots_inventory']) + \
        sum(c.get('case_dir_inventory', {}).get('copy_bytes', 0) for c in cases)
    st = os.statvfs(str(args.dest))
    free = st.f_bavail * st.f_frsize
    print('DEST_FREE_GB={:.1f} NEED_GB~={:.1f}'.format(free / 1024 ** 3, need / 1024 ** 3), flush=True)
    require(free > need * 1.05 + 2 * 1024 ** 3, 'Not enough space on the Research destination')
    session = args.dest / ('legacy1000_' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '_' + uuid.uuid4().hex[:8])
    session.mkdir()
    (session / 'plan.json').write_text(json.dumps(plan, indent=1))
    print('SESSION', session, flush=True)
    ok, failed = [], []
    for k, entry in enumerate(cases, 1):
        name = entry['case']
        t0 = time.time()
        print('ARCHIVING\t{}/{}\t{}'.format(k, len(cases), name), flush=True)
        try:
            recheck_case(entry, plan, args)
            tar_path, rows = write_case_tar(entry, session)
            n = O.verify_tar(tar_path, [r for r in rows if r[0] != 'nocopy'])
            receipt = {'case': name, 'tar': tar_path.name, 'tar_sha256': O.sha256_file(str(tar_path)),
                       'tar_size': tar_path.stat().st_size, 'members': n,
                       'nocopy_entries': sum(1 for r in rows if r[0] == 'nocopy'),
                       'verified_utc': now_utc(), 'host': socket.gethostname(), 'version': VERSION}
            (session / (name + '.VERIFIED')).write_text(json.dumps(receipt, indent=1))
            print('ARCHIVED_VERIFIED\t{}\tmembers={}\tnot_copied_beyond1000={}\tGB={:.2f}\tseconds={:.0f}'.format(
                name, n, receipt['nocopy_entries'], receipt['tar_size'] / 1024 ** 3, time.time() - t0), flush=True)
            ok.append(name)
        except (Stop, OSError, tarfile.TarError, RuntimeError) as exc:
            print('ARCHIVE_SKIPPED\t{}\t{}'.format(name, exc), flush=True)
            leftover = session / (name + '.tar.partial')
            if leftover.exists():
                leftover.unlink()
            failed.append(name)
    print('\nARCHIVE_DONE session={} verified={} skipped={}'.format(session, len(ok), len(failed)))
    print('Nothing was deleted. Next: clean --session {} --confirm {}'.format(session, TOKEN))
    return 0 if ok and not failed else 1


def do_verify(args):
    session = args.session
    receipts = sorted(session.glob('*.VERIFIED'))
    require(receipts, 'No receipts in ' + str(session))
    bad = 0
    for rec in receipts:
        r = json.loads(rec.read_text())
        try:
            rows = [x for x in O.read_manifest(session / (r['case'] + '.manifest.tsv')) if x[0] != 'nocopy']
            tar_path = session / r['tar']
            require(O.sha256_file(str(tar_path)) == r['tar_sha256'], 'tar SHA256 differs from receipt')
            print('VERIFIED\t{}\tmembers={}'.format(r['case'], O.verify_tar(tar_path, rows)), flush=True)
        except (Stop, OSError, tarfile.TarError) as exc:
            bad += 1
            print('VERIFY_FAILED\t{}\t{}'.format(r['case'], exc), flush=True)
    print('VERIFY_SESSION {} ok={} failed={}'.format(session, len(receipts) - bad, bad))
    return 1 if bad else 0


# --------------------------------------------------------------------------- clean
def remove_empty_tree(root, name, log):
    n = 0
    if not root.exists():
        return 0
    for folder, _, _ in os.walk(str(root), topdown=False, followlinks=False):
        try:
            os.rmdir(folder)
            n += 1
            log.write('{}\trmdir\t{}\t{}\n'.format(now_utc(), name, folder))
        except OSError:
            pass
    parent = root.parent
    if parent.name == name and parent.exists():
        try:
            os.rmdir(str(parent))
            n += 1
            log.write('{}\trmdir\t{}\t{}\n'.format(now_utc(), name, parent))
        except OSError:
            pass
    return n


def do_clean(args):
    require(args.confirm == TOKEN, 'clean requires --confirm ' + TOKEN)
    session = args.session
    plan = load_plan(session / 'plan.json')
    O.check_dest(session, plan['scratch_roots'], args.allow_non_nfs_dest)
    log = (session / 'cleanup_actions.tsv').open('a')
    tot_f = tot_d = tot_k = 0
    for entry in plan['cases']:
        name = entry['case']
        if args.case and name not in args.case:
            continue
        rec = session / (name + '.VERIFIED')
        if not rec.exists():
            print('CLEAN_SKIPPED\t{}\tno VERIFIED receipt'.format(name), flush=True)
            continue
        r = json.loads(rec.read_text())
        tar_path = session / r['tar']
        try:
            require(tar_path.stat().st_size == r['tar_size'], 'tar size differs from receipt')
            if args.rehash:
                require(O.sha256_file(str(tar_path)) == r['tar_sha256'], 'tar SHA256 differs from receipt')
            recheck_case(entry, plan, args)
        except (Stop, OSError, RuntimeError) as exc:
            print('CLEAN_SKIPPED\t{}\t{}'.format(name, exc), flush=True)
            continue
        roots = [Path(p) for p in entry['delete_roots']]
        case_dir = Path(entry['case_dir'])
        rows = [x for x in O.read_manifest(session / (name + '.manifest.tsv')) if x[0] in ('delete', 'nocopy')]
        # Outputs first, the case directory last (so a crash leaves the case readable).
        rows.sort(key=lambda x: is_within(Path(x[2]), case_dir))
        files = changed = 0
        for role, kind, p, size, mtime, extra in rows:
            require(any(is_within(Path(p), rt) for rt in roots), 'manifest path outside delete roots: ' + p)
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
                log.write('{}\tunlink_{}\t{}\t{}\n'.format(now_utc(), role, name, p))
            else:
                changed += 1
                log.write('{}\tKEPT_CHANGED\t{}\t{}\n'.format(now_utc(), name, p))
        dirs = 0
        for rt in sorted(roots, key=lambda x: is_within(x, case_dir) or x == case_dir):
            dirs += remove_empty_tree(rt, name, log)
        log.flush()
        left = sum(1 for rt in roots if rt.exists())
        print('CLEANED\t{}\tunlinked={}\trmdir={}\tkept_changed={}\troots_left={}'.format(
            name, files, dirs, changed, left), flush=True)
        tot_f, tot_d, tot_k = tot_f + files, tot_d + dirs, tot_k + changed
    log.close()
    print('\nCLEAN_DONE unlinked={} rmdir={} kept_changed={} log={}'.format(
        tot_f, tot_d, tot_k, session / 'cleanup_actions.tsv'))
    print('Run myfiles for the real Scratch quota.')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--version', action='version', version=VERSION)
    p.add_argument('mode', choices=['plan', 'probe-mount', 'archive', 'verify', 'clean'])
    p.add_argument('--case-root', type=Path, default=A.DEFAULT_ROOT)
    p.add_argument('--out-root', type=Path, default=DEFAULT_OUT_ROOT, help='CIME output root (orphan report only)')
    p.add_argument('--scratch-root', action='append', default=None)
    p.add_argument('--sites', nargs='+', help='plan: only these legacy sites')
    p.add_argument('--exclude-case', nargs='+', default=[], help='plan: leave these cases alone')
    p.add_argument('--extra-case', nargs='+', default=[], help='plan: also offload these exact cases')
    p.add_argument('--copy-beyond-1000', action='store_true', help='plan: copy output beyond 1000 years too')
    p.add_argument('--keep-case-dirs', action='store_true', help='plan: archive case dirs but keep them on Scratch')
    p.add_argument('--plan', type=Path)
    p.add_argument('--session', type=Path)
    p.add_argument('--case', action='append', help='archive/clean only these case(s) from the plan')
    p.add_argument('--dest', type=Path, default=DEFAULT_DEST)
    p.add_argument('--quiet-minutes', type=float, default=120)
    p.add_argument('--confirm', default='')
    p.add_argument('--rehash', action='store_true', help='clean: re-hash each tar before deleting (slower)')
    p.add_argument('--allow-non-nfs-dest', action='store_true', help=argparse.SUPPRESS)  # tests only
    args = p.parse_args(argv)
    args.scratch_root = args.scratch_root or list(O.DEFAULT_SCRATCH_ROOTS)
    try:
        if args.mode == 'plan':
            plan = build_plan(args)
            print_plan(plan)
            PLAN_DIR.mkdir(exist_ok=True)
            out = PLAN_DIR / ('legacy1000_plan_' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '.json')
            out.write_text(json.dumps(plan, indent=1))
            print('PLAN_WRITTEN', out)
            print('READ ONLY: nothing archived or deleted.')
            return 0
        if args.mode == 'probe-mount':
            O.check_dest(args.dest, args.scratch_root, True)
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
