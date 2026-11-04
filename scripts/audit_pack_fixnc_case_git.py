#!/usr/bin/env python3
"""Find FINISHED FIXNC (fixed-biogeography / no-competition) CIME cases and pack
their per-case Git objects to free Scratch inodes.

Scope (Non-Competition-CLM-experiment):
  ICOS  Batch01-Batch04   case names  <SITE>_B0N_FIXNC_...
  AmeriFlux Batch01-05    case names  <SITE>_AMF_B0N_FIXNC_...  (Batch01: <SITE>_AMF_FIXNC_...)
The 38 legacy 6/8/11/13-site SPINUP cases are only REPORTED here; pack them with
pack_four_batch_case_git.py. NL-Loo / DE-Tha OBSPFT_NC examples are report-only
unless named with --case.

Modes
  audit (default, READ ONLY): per-case completion state + Git inode/loose-object
        statistics + per-cohort summary. Opens no NetCDF; writes nothing.
  run:  `git gc --no-prune` ONLY for cases judged DONE (or any idle state with
        --include-idle), unlocked, with no related Slurm job, unchanged for
        --quiet-minutes, verified by a full Git snapshot before and after.
        Never deletes case folders, .git, outputs, restarts, locks or reflogs.

Completion is judged from CIME's own records, not the case name:
  CaseStatus last case.run/st_archive event, RESUBMIT in env_run.xml, Slurm
  queue, and the newest archived restart set in $DOUT_S_ROOT/rest compared with
  the model years implied by the case name (AD400/NORMAL400 -> 400, DIAG50/
  FIXA50 -> 50, ..._AD5 -> 5). This is NOT a scientific convergence check.

Python 3.9+, Linux/IRIDIS6, no third-party modules, no network.
"""
import argparse
from collections import Counter, OrderedDict
from contextlib import contextmanager
import getpass
import hashlib
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

VERSION = '20261104-v3'
DEFAULT_ROOT = Path('/iridisfs/scratch/ly3n24/CLM_FATES/cases')

# Site manifests: Non-Competition-CLM-experiment README / examples (Nov 2026)
# and CLM-FATES-single-site-workflows examples/AmeriFlux_MultiSite_10*/sites.tsv.
ICOS = OrderedDict([
    ('B01', 'CZ-BK1 IT-Ren RU-Fyo CH-Dav IT-Col FR-Fon FI-Sod FR-Pue DE-Hai BE-Vie'.split()),
    ('B02', 'DK-Sor FR-Hes FR-FBn BE-Bra IT-Noe CH-Fru CH-Lae ES-LJu FI-Hyy IT-MBo'.split()),
    ('B03', 'IT-Lav FI-Let IL-Yat DE-Obe CZ-Stn DE-Lnf DE-Hzd GF-Guy AT-Neu CH-Cha'.split()),
    ('B04', 'CH-Aws DE-Gri DE-RuR IT-Tor ES-Agu GL-ZaH'.split()),
])
AMF = OrderedDict([
    ('B01', 'BR-Ma2 CA-Ca3 CA-Cbo CA-LP1 CA-Man CA-TP1 CA-TP3 CA-TP4 US-Aud US-Bar'.split()),
    ('B02', 'US-BZS US-CMW US-EML US-GLE US-Ha1 US-Ho1 US-Ho2 US-Ho3 US-ICh US-ICt'.split()),
    ('B03', 'US-Kon US-Me2 US-Me6 US-MMS US-MOz US-Mpj US-MtB US-NC2 US-NR1 US-NR3'.split()),
    ('B04', 'US-Rpf US-Seg US-Ses US-Slt US-SP1 US-SRG US-SRM US-Syv US-Ton US-Uaf'.split()),
    ('B05', 'US-UiD US-UMB US-UMd US-Var US-Vcm US-Vcp US-WCr US-Whs US-Wjs US-Wkg'.split()),
])
# Same 38-case whitelist as pack_four_batch_case_git.py (VERSION 20261026-v2).
_LEGACY_BATCHES = {
    '6': 'DE-Hai RU-Fyo DE-Tha DK-Sor NL-Loo IL-Yat'.split(),
    '8': 'CZ-BK1 IT-Ren FR-Hes FR-FBn DE-Hzd CH-Dav BE-Vie IT-Lav'.split(),
    '11': 'BE-Bra FR-Fon GL-ZaH IT-Noe AT-Neu FI-Sod CH-Aws CH-Cha CH-Fru CH-Lae CZ-Stn'.split(),
    '13': 'DE-Lnf DE-Obe ES-Agu ES-LJu IT-Col DE-Gri DE-RuR FI-Hyy FI-Let FR-Pue GF-Guy IT-MBo IT-Tor'.split(),
}
_LEGACY_OVERRIDES = {'IT-Noe': 'IT-Noe_ERA5_ABW_BRANCH2009',
                     'CH-Aws': 'CH-Aws_FATES_SPINUP1000_RH42A_V8_CONT2016'}
LEGACY = {}
for _b, _sites in _LEGACY_BATCHES.items():
    _suf = '_FATES_SPINUP500_11P' if _b == '11' else '_FATES_SPINUP1000_S30SHARED' if _b == '13' else '_FATES_SPINUP500'
    for _s in _sites:
        LEGACY[_LEGACY_OVERRIDES.get(_s, _s + _suf)] = _b
PROTECTED_PREFIXES = ('NL-Loo_OBSPFT_NC', 'DE-Tha_OBSPFT_NC')

SCOPED = ['ICOS_' + b for b in ICOS] + ['AMF_' + b for b in AMF]
IDLE_STATES = ('DONE', 'STOPPED_SHORT', 'ENDED_NO_REST', 'STALLED', 'ARCHIVE_PENDING',
               'ERROR', 'INTERRUPTED', 'NEVER_RUN')
NAME_RE = re.compile(r'^(?P<site>[A-Z]{2}-[A-Za-z0-9]{2,3})_(?P<rest>.+)$')
TARGET_RE = re.compile(r'(?:^|_)(?:AD|NORMAL|DIAG|FIX[A-Z]|SPINUP|PRE|POST)(\d+)(?=_|$)')
EVENT_RE = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d): (\S+) (starting|success|error)\b')
REST_RE = re.compile(r'^(\d{4,})-(\d\d)-(\d\d)-(\d{5})$')
CIME_JOB_RE = re.compile(r'^(?:run|st_archive|case\.run|case\.st_archive|case\.test)\.(.+)$')


# --------------------------------------------------------------------------- helpers
def command(args, check=True):
    env = os.environ.copy()
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        env.pop(key, None)
    env['GIT_OPTIONAL_LOCKS'] = '0'   # status/diff must not rewrite the index
    result = subprocess.run([str(a) for a in args], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError('{}: {}'.format(' '.join(map(str, args)),
                           result.stderr.decode(errors='replace').strip()[-800:]))
    return result.stdout


def git(case, *args):
    return command(['git', '-C', case, *args])


def classify(name):
    """Return (cohort, note). Cohort tag comes from the name; site list is a cross-check."""
    if name in LEGACY:
        return 'LEGACY_' + LEGACY[name], ''
    if name.startswith(PROTECTED_PREFIXES):
        return 'PROTECTED_EXAMPLE', ''
    m = NAME_RE.match(name)
    if not m:
        return 'OTHER', ''
    site, rest = m.group('site'), m.group('rest')
    # Batch tag may carry a suffix, e.g. AMF_B01SIX_FIXNC_ (AmeriFlux Batch01 six-input cases)
    tag = re.match(r'^(AMF_|ICOS_)?(?:(B0[1-5])[A-Za-z0-9]*_)?FIXNC_', rest)
    if not tag:
        return 'OTHER', ''
    if tag.group(1) == 'AMF_':
        batch = tag.group(2) or 'B01'
        sites = AMF.get(batch, [])
        return 'AMF_' + batch, '' if site in sites else 'SITE_NOT_IN_MANIFEST'
    if tag.group(2):
        batch = tag.group(2)
        sites = ICOS.get(batch, [])
        return 'ICOS_' + batch, '' if site in sites else 'SITE_NOT_IN_MANIFEST'
    return 'OTHER', 'FIXNC_WITHOUT_BATCH_TAG'


def target_years(name, cohort):
    if cohort.startswith('LEGACY'):
        return None          # legacy names do not encode the final year count
    found = TARGET_RE.findall(name)
    return int(found[-1]) if found else None


def read_env(case):
    env = {}
    for fname in ('env_case.xml', 'env_build.xml', 'env_run.xml'):
        path = case / fname
        if not path.is_file():
            continue
        try:
            root = ET.parse(str(path)).getroot()
        except ET.ParseError:
            continue
        for entry in root.iter('entry'):
            key = entry.get('id')
            if not key:
                continue
            value = entry.get('value')
            if value is None:
                child = entry.find('value')
                value = child.text if child is not None else None
            if value is not None:
                env[key] = value.strip()
    env.setdefault('CASE', case.name)
    env.setdefault('CASEROOT', str(case))
    return env


def expand(value, env):
    if not value:
        return value
    pattern = re.compile(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?')
    for _ in range(10):
        new = pattern.sub(lambda m: env.get(m.group(1), os.environ.get(m.group(1), m.group(0))), value)
        if new == value:
            break
        value = new
    return value


def read_events(case):
    path = case / 'CaseStatus'
    if not path.is_file():
        return None
    events = []
    with path.open('r', errors='replace') as handle:
        for line in handle:
            m = EVENT_RE.match(line)
            if m:
                events.append(m.groups())
    return events


def rest_years(dout_s_root):
    if not dout_s_root:
        return None
    rest = Path(dout_s_root) / 'rest'
    try:
        names = os.listdir(str(rest))
    except OSError:
        return None
    years = [int(m.group(1)) for m in (REST_RE.match(n) for n in names) if m]
    return max(years) if years else None


def git_stats(case):
    """Read-only. Inode/loose counts; never runs commands that write."""
    gd = case / '.git'
    out = {'git_inodes': None, 'loose': None, 'in_pack': None, 'packs': None,
           'fanout': None, 'locks': [], 'git_error': ''}
    if gd.is_symlink() or not gd.is_dir():
        out['git_error'] = 'NO_PRIVATE_GIT'
        return out
    inodes = set()
    try:
        for folder, dirs, files in os.walk(str(gd), followlinks=False):
            for item in [folder] + [os.path.join(folder, x) for x in files + dirs]:
                st = os.lstat(item)
                inodes.add((st.st_dev, st.st_ino))
                base = os.path.basename(item)
                if base == 'gc.pid' or base.endswith('.lock'):
                    out['locks'].append(item)
        out['git_inodes'] = len(inodes)
        out['fanout'] = sum(1 for n in os.listdir(str(gd / 'objects'))
                            if re.fullmatch(r'[0-9a-f]{2}', n))
        counts = {}
        for line in git(case, 'count-objects', '-v').decode().splitlines():
            key, _, value = line.partition(': ')
            counts[key] = value
        out['loose'] = int(counts.get('count', 0))
        out['in_pack'] = int(counts.get('in-pack', 0))
        out['packs'] = int(counts.get('packs', 0))
    except (OSError, RuntimeError, ValueError) as exc:
        out['git_error'] = 'GIT_STATS_FAILED: ' + str(exc)[:200]
    return out


def mtime_age_minutes(case):
    newest = 0.0
    for rel in ('CaseStatus', 'env_run.xml', '.git', '.git/index', '.git/logs/HEAD', '.git/HEAD'):
        try:
            newest = max(newest, (case / rel).stat().st_mtime)
        except OSError:
            pass
    return (time.time() - newest) / 60.0 if newest else None


def count_entries(path):
    """Count directory entries recursively without stat()-ing files (d_type)."""
    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    total += 1
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        pass
        except OSError:
            pass
    return total


# --------------------------------------------------------------------------- Slurm
def read_queue():
    raw = command(['squeue', '-h', '-u', getpass.getuser(), '-o', '%i|%j|%T|%Z|%o'])
    own = os.environ.get('SLURM_JOB_ID', '')
    jobs = []
    for line in raw.decode(errors='replace').splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split('|', 4)]
        if len(parts) != 5:
            raise RuntimeError('Unrecognized squeue output: ' + line)
        if parts[0] != own and parts[0].split('_')[0] != own:
            jobs.append(dict(zip(('id', 'name', 'state', 'workdir', 'command'), parts)))
    return jobs


def token_re(name):
    # case name as a whole token: "run.CASE", "/cases/CASE/" match; "CASE_DIAG50" does not
    return re.compile(r'(?<![A-Za-z0-9_-])' + re.escape(name) + r'(?![A-Za-z0-9_-])')


def relate_jobs(jobs, cases):
    """Map case name -> [job ids]; return also jobs not attributable to any case."""
    related = {name: [] for name in cases}
    unknown = []
    patterns = {name: token_re(name) for name in cases}
    for job in jobs:
        hit = False
        m = CIME_JOB_RE.match(job['name'])
        if m and m.group(1) in related:
            related[m.group(1)].append(job['id'])
            hit = True
        text = ' '.join((job['name'], job['workdir'], job['command']))
        for name, pat in patterns.items():
            if pat.search(text) and job['id'] not in related[name]:
                related[name].append(job['id'])
                hit = True
        if not hit and not m:
            unknown.append(job)
    return related, unknown


# --------------------------------------------------------------------------- per-case state
def evaluate(case, cohort, note, related_jobs, quiet_minutes, queue_ok, measure=False):
    name = case.name
    env = read_env(case)
    events = read_events(case)
    resubmit = env.get('RESUBMIT')
    try:
        resubmit_i = int(resubmit) if resubmit is not None else None
    except ValueError:
        resubmit_i = None
    dout_s = env.get('DOUT_S', 'TRUE').upper() == 'TRUE'
    dout_root = expand(env.get('DOUT_S_ROOT'), env)
    start = env.get('RUN_STARTDATE', '')
    start_year = int(start[:4]) if re.match(r'^\d{4}', start) else None
    last_rest = rest_years(dout_root)
    elapsed = (last_rest - start_year) if (last_rest is not None and start_year is not None) else None
    target = target_years(name, cohort)
    run_events = [e for e in (events or []) if e[1] in ('case.run', 'st_archive')]
    last = run_events[-1] if run_events else None
    age = mtime_age_minutes(case)
    g = git_stats(case)

    if not queue_ok:
        state = 'QUEUE_UNKNOWN'
    elif related_jobs:
        state = 'ACTIVE'
    elif events is None:
        state = 'NO_CASESTATUS'
    elif g['locks']:
        state = 'GIT_LOCKED'
    elif age is not None and age < quiet_minutes:
        state = 'RECENT'
    elif last is None:
        state = 'NEVER_RUN'
    elif last[2] == 'error':
        state = 'ERROR'
    elif last[2] == 'starting':
        state = 'INTERRUPTED'          # job gone without success: timeout/kill/node fail
    elif last[1] == 'case.run' and dout_s:
        state = 'ARCHIVE_PENDING'      # model segment finished, st_archive never succeeded
    elif resubmit_i is None or resubmit_i > 0:
        state = 'STALLED'              # resubmission chain broken / cancelled dependency
    elif target is not None and elapsed is None:
        state = 'ENDED_NO_REST'
    elif target is not None and elapsed < target:
        state = 'STOPPED_SHORT'
    else:
        state = 'DONE'

    est = 0
    if g['loose']:
        est = max(0, g['loose'] + (g['fanout'] or 0) - 3)
    info = OrderedDict([
        ('cohort', cohort), ('state', state), ('case', name),
        ('resubmit', resubmit if resubmit is not None else '?'),
        ('last_event', '{} {} {}'.format(last[0], last[1], last[2]) if last else '-'),
        ('start_year', start_year if start_year is not None else '?'),
        ('last_rest_year', last_rest if last_rest is not None else '?'),
        ('years', '{}/{}'.format('?' if elapsed is None else elapsed, '?' if target is None else target)),
        ('git_inodes', '?' if g['git_inodes'] is None else g['git_inodes']),
        ('loose', '?' if g['loose'] is None else g['loose']),
        ('packs', '?' if g['packs'] is None else g['packs']),
        ('locks', len(g['locks'])),
        ('est_reclaim', est),
        ('jobs', ','.join(related_jobs) or '-'),
        ('note', ';'.join(x for x in (note, g['git_error']) if x) or '-'),
    ])
    if measure:
        info['case_entries_excl_git'] = count_entries(str(case)) - (g['git_inodes'] or 0)
        for key, var in (('rundir_entries', 'RUNDIR'), ('bld_entries', 'EXEROOT'), ('archive_entries', 'DOUT_S_ROOT')):
            path = expand(env.get(var), env)
            info[key] = count_entries(path) if path and os.path.isdir(path) else '-'
    return info, g


def discover(root):
    cases = []
    skipped = []
    with os.scandir(str(root)) as it:
        for entry in it:
            if entry.is_symlink():
                skipped.append(entry.name + ' (symlink)')
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
            p = Path(entry.path)
            if (p / 'env_run.xml').is_file() or (p / 'CaseStatus').is_file():
                cases.append(p)
    return sorted(cases, key=lambda p: p.name), skipped


def other_maintenance_running():
    """Non-destructive probe of the shared Home advisory lock."""
    import fcntl
    lock = Path.home() / '.cime_git_maintenance.lock'
    if not lock.exists():
        return False
    with lock.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def survey(args):
    root = args.case_root.resolve(strict=True)
    paths, skipped = discover(root)
    queue_ok, jobs, queue_error = True, [], ''
    try:
        jobs = read_queue()
    except (OSError, RuntimeError) as exc:
        queue_ok, queue_error = False, str(exc)
    names = [p.name for p in paths]
    related, unknown = relate_jobs(jobs, names) if queue_ok else ({n: [] for n in names}, [])
    rows = []
    for p in paths:
        cohort, note = classify(p.name)
        detailed = (cohort in args.cohorts or p.name in (args.cases or [])
                    or (args.name_re is not None and args.name_re.search(p.name) and not cohort.startswith('LEGACY')))
        if detailed:
            info, g = evaluate(p, cohort, note, related[p.name], args.quiet_minutes, queue_ok, args.measure)
        else:
            g = git_stats(p)
            info = OrderedDict([('cohort', cohort), ('state', '-'), ('case', p.name),
                                ('git_inodes', g['git_inodes'] or 0), ('loose', g['loose'] or 0)])
        rows.append((p, info, g, detailed))
    return root, rows, jobs, unknown, queue_ok, queue_error, skipped


def pack_decision(info, g, args):
    """Return 'WOULD_PACK' or a SKIP reason."""
    cohort = info['cohort']
    explicit = info['case'] in (args.cases or []) or bool(args.name_re and args.name_re.search(info['case']))
    if cohort.startswith('LEGACY'):
        return 'SKIP_LEGACY_USE_pack_four_batch_case_git.py'
    if cohort in ('PROTECTED_EXAMPLE', 'OTHER') and not explicit:
        return 'SKIP_' + cohort
    allowed = IDLE_STATES if args.include_idle else ('DONE',)
    if info['state'] not in allowed:
        return 'SKIP_STATE_' + info['state']
    if g['locks']:
        return 'SKIP_LOCKED'
    if g['git_error']:
        return 'SKIP_GIT_ERROR'
    if (g['loose'] or 0) < args.min_loose:
        return 'SKIP_ALREADY_PACKED' if (g['loose'] or 0) == 0 else 'SKIP_FEW_LOOSE'
    return 'WOULD_PACK'


def print_report(root, rows, jobs, unknown, queue_ok, queue_error, skipped, args, out=sys.stdout):
    print('FIXNC CASE COMPLETION + GIT AUDIT {}  host={}  UTC={}'.format(
        VERSION, socket.gethostname(), datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')), file=out)
    print('case_root={}  cases_found={}  quiet_minutes={}  min_loose={}'.format(
        root, len(rows), args.quiet_minutes, args.min_loose), file=out)
    if not queue_ok:
        print('QUEUE_UNKNOWN: {} -> no case can be judged idle'.format(queue_error), file=out)
    else:
        print('slurm_jobs={}  unattributed_jobs={}'.format(len(jobs), len(unknown)), file=out)
        for job in unknown:
            print('  UNATTRIBUTED_JOB {id} {name} {state} WorkDir={workdir}'.format(**job), file=out)
    for item in skipped:
        print('  SKIPPED_ENTRY ' + item, file=out)
    try:
        if other_maintenance_running():
            print('WARNING: ~/.cime_git_maintenance.lock is HELD (another Git maintenance job, e.g. gc_finished_nc, is running)', file=out)
    except OSError:
        pass

    detail = [(p, i, g) for p, i, g, d in rows if d]
    if detail:
        cols = list(detail[0][1].keys()) + ['action']
        print('\n' + '\t'.join(cols), file=out)
        for _, info, g in sorted(detail, key=lambda r: (r[1]['cohort'], r[1]['state'], r[1]['case'])):
            info['action'] = pack_decision(info, g, args)
            print('\t'.join(str(info[c]) for c in cols), file=out)

    print('\nCOHORT SUMMARY (git_inodes / loose objects; est = estimated inodes freed by packing)', file=out)
    by = OrderedDict()
    for _, info, g, d in rows:
        by.setdefault(info['cohort'], []).append((info, g, d))
    order = SCOPED + sorted(k for k in by if k not in SCOPED)
    print('{:<18} {:>5}  {:<52} {:>10} {:>9} {:>10} {:>10}'.format(
        'cohort', 'cases', 'states', 'git_inodes', 'loose', 'est_DONE', 'would_pack'), file=out)
    tot = Counter()
    for cohort in order:
        items = by.get(cohort)
        if not items:
            if cohort in SCOPED:
                print('{:<18} {:>5}  {}'.format(cohort, 0, '(no case directories found)'), file=out)
            continue
        states = Counter(i['state'] for i, _, _ in items)
        gi = sum(int(i['git_inodes']) for i, _, _ in items if str(i['git_inodes']).isdigit())
        lo = sum(int(i['loose']) for i, _, _ in items if str(i['loose']).isdigit())
        est_done = sum(i.get('est_reclaim', 0) for i, _, _ in items if i['state'] == 'DONE')
        wp = sum(1 for i, _, _ in items if i.get('action') == 'WOULD_PACK')
        st = ' '.join('{}={}'.format(k, v) for k, v in sorted(states.items())) if states != Counter({'-': len(items)}) else '(not evaluated)'
        print('{:<18} {:>5}  {:<52} {:>10} {:>9} {:>10} {:>10}'.format(
            cohort, len(items), st[:52], gi, lo, est_done, wp), file=out)
        tot.update(cases=len(items), gi=gi, lo=lo, est=est_done, wp=wp)
    print('{:<18} {:>5}  {:<52} {:>10} {:>9} {:>10} {:>10}'.format(
        'TOTAL', tot['cases'], '', tot['gi'], tot['lo'], tot['est'], tot['wp']), file=out)
    unscoped = sorted(((int(i['loose']), i['cohort'], i['case']) for _, i, g, d in rows
                       if not d and str(i['loose']).isdigit() and int(i['loose']) >= args.min_loose), reverse=True)
    if unscoped:
        print('\nNOT EVALUATED, >= {} loose objects (check naming; legacy -> pack_four_batch_case_git.py; '
              'others need --case NAME):'.format(args.min_loose), file=out)
        for loose, cohort, name in unscoped:
            print('  UNSCOPED\t{}\t{}\t{}'.format(cohort, name, loose), file=out)
    print('\nStates: DONE=RESUBMIT 0 + last st_archive success + archived years reach name target; '
          'STOPPED_SHORT=ended cleanly below target; STALLED=success but RESUBMIT>0 and no job; '
          'ARCHIVE_PENDING/INTERRUPTED/ERROR=idle but not finished; ACTIVE/RECENT/GIT_LOCKED=do not touch.', file=out)
    print('READ ONLY: nothing was changed.' if args.mode == 'audit' else '', file=out)


# --------------------------------------------------------------------------- packing (run)
def case_git(case):
    if case.is_symlink() or not case.is_dir() or case.resolve() != case:
        raise RuntimeError('Missing case or symlink case path: ' + str(case))
    gd = case / '.git'
    if gd.is_symlink() or not gd.is_dir() or gd.resolve() != gd:
        raise RuntimeError('Expected a private .git directory: ' + str(gd))
    if gd.stat().st_uid != os.getuid():
        raise RuntimeError('Case Git is owned by another user: ' + str(gd))
    if not (case / 'CaseStatus').is_file() or not (case / 'env_run.xml').is_file():
        raise RuntimeError('Missing CIME CaseStatus/env_run.xml: ' + str(case))
    top = Path(os.fsdecode(git(case, 'rev-parse', '--show-toplevel')).strip()).resolve()
    common = Path(os.fsdecode(git(case, 'rev-parse', '--git-common-dir')).strip())
    common = common if common.is_absolute() else case / common
    if top != case or common.resolve() != gd:
        raise RuntimeError('Git root/shared worktree mismatch: ' + case.name)
    if (gd / 'objects').is_symlink() or (gd / 'objects/info/alternates').exists() or (gd / 'modules').exists():
        raise RuntimeError('Shared object storage/submodules need separate review: ' + case.name)
    for folder, dirs, files in os.walk(str(gd), followlinks=False):
        for x in files + dirs:
            p = Path(folder) / x
            if p.is_symlink():
                raise RuntimeError('Symlink inside .git needs separate review: ' + str(p))
            if x == 'gc.pid' or x.endswith('.lock'):
                raise RuntimeError('Git lock present (never removed automatically): ' + str(p))
    return gd


def snapshot(case):
    objects = sorted(git(case, 'cat-file', '--batch-all-objects',
                         '--batch-check=%(objectname)').splitlines())
    index = case / '.git/index'
    return {
        'HEAD': git(case, 'rev-parse', 'HEAD'),
        'refs': git(case, 'for-each-ref', '--format=%(refname) %(objectname)'),
        'reflogs': git(case, 'reflog', 'show', '--all', '--format=%gD %H %gs'),
        'index': hashlib.sha256(index.read_bytes()).digest() if index.exists() else None,
        'status': git(case, 'status', '--porcelain=v1', '-z', '--untracked-files=all'),
        'casestatus': hashlib.sha256((case / 'CaseStatus').read_bytes()).digest(),
        'all_object_ids': hashlib.sha256(b'\n'.join(objects)).digest(),
        'n_objects': len(objects),
    }


@contextmanager
def maintenance_lock():
    import fcntl
    with (Path.home() / '.cime_git_maintenance.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another Git maintenance invocation (e.g. gc_finished_nc) holds '
                               '~/.cime_git_maintenance.lock; not overlapping.') from exc
        yield


def run(args):
    with maintenance_lock():
        root, rows, jobs, unknown, queue_ok, queue_error, skipped = survey(args)
        print_report(root, rows, jobs, unknown, queue_ok, queue_error, skipped, args)
        if not queue_ok:
            raise RuntimeError('Slurm queue unreadable; refusing to pack.')
        if unknown and not args.allow_unknown_jobs:
            raise RuntimeError('{} Slurm job(s) cannot be attributed to a case (listed above as '
                               'UNATTRIBUTED_JOB). A controller job may modify cases. Re-run with '
                               '--allow-unknown-jobs after checking them.'.format(len(unknown)))
        targets = [(p, i) for p, i, g, d in rows if d and i.get('action') == 'WOULD_PACK']
        if args.max_cases:
            targets = targets[:args.max_cases]
        print('\nPACK SELECTION: {} case(s)'.format(len(targets)), flush=True)
        if not targets:
            print('NOTHING_TO_PACK: no repositories changed')
            return 0
        done, failed, total_before, total_after = [], [], 0, 0
        for path, _ in targets:
            name = path.name
            try:
                # Re-evaluate everything immediately before touching this case.
                jobs_now = read_queue()
                rel, _ = relate_jobs(jobs_now, [name])
                cohort, note = classify(name)
                info, g = evaluate(path, cohort, note, rel[name], args.quiet_minutes, True)
                reason = pack_decision(info, g, args)
                if reason != 'WOULD_PACK':
                    raise RuntimeError('state changed: ' + reason)
                case_git(path)
                git(path, 'fsck', '--connectivity-only', '--no-dangling')
                snap = snapshot(path)
            except (OSError, RuntimeError) as exc:
                print('SKIP {}: {}'.format(name, exc), flush=True)
                failed.append(name)
                continue
            before = g['git_inodes']
            print('BEFORE\t{}\t{}\tgit_inodes={}\tloose={}\tpacks={}'.format(
                cohort, name, before, g['loose'], g['packs']), flush=True)
            # Same options as pack_four_batch_case_git.py: keep reflogs/unreachable
            # objects; never --force/--aggressive/--prune=now/--no-detach.
            command(['nice', '-n', '10', 'git', '-C', path,
                     '-c', 'gc.reflogExpire=never',
                     '-c', 'gc.reflogExpireUnreachable=never',
                     '-c', 'gc.worktreePruneExpire=never',
                     '-c', 'gc.packRefs=false', '-c', 'gc.autoDetach=false',
                     '-c', 'pack.threads=1', 'gc', '--no-prune'])
            # Post-pack validation: any difference is a hard STOP for the whole run.
            case_git(path)
            git(path, 'fsck', '--connectivity-only', '--no-dangling')
            if snapshot(path) != snap:
                raise RuntimeError('POST-PACK VALIDATION FAILED (Git state/objects differ): ' + name +
                                   '. STOPPING. Inspect this case manually.')
            g2 = git_stats(path)
            print('AFTER\t{}\t{}\tgit_inodes={}\tloose={}\tpacks={}\treclaimed={}'.format(
                cohort, name, g2['git_inodes'], g2['loose'], g2['packs'],
                before - g2['git_inodes']), flush=True)
            total_before += before
            total_after += g2['git_inodes']
            done.append(name)
        print('\nCOMPLETE packed={} git_inodes_before={} git_inodes_after={} reclaimed={}'.format(
            len(done), total_before, total_after, total_before - total_after))
        if failed:
            print('PARTIAL skipped_at_recheck={} cases={}'.format(len(failed), ','.join(failed)))
        print('Run `myfiles` for the real Scratch quota. No model output/restart files were opened.')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('mode', nargs='?', choices=['audit', 'run'], default='audit')
    parser.add_argument('--case-root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--cohort', action='append', dest='cohort_list',
                        choices=SCOPED + ['PROTECTED_EXAMPLE', 'OTHER'],
                        help='Restrict detailed evaluation/packing (repeatable). Default: all ICOS_B01-04 + AMF_B01-05')
    parser.add_argument('--case', action='append', dest='cases',
                        help='Exact case name(s) to evaluate/pack; also allows PROTECTED_EXAMPLE/OTHER cases')
    parser.add_argument('--name-regex', default=None,
                        help="Evaluate/pack cases whose name matches this regex (any FIXNC cohort, incl. protected "
                             "examples; never legacy), e.g. '_AD400.*_(NLF|NLFP|PHEN|HINONE)$'")
    parser.add_argument('--include-idle', action='store_true',
                        help='Also pack idle-but-unfinished cases (STOPPED_SHORT, STALLED, ERROR, ...). Default: DONE only')
    parser.add_argument('--min-loose', type=int, default=200,
                        help='Pack only if at least this many loose objects (default 200)')
    parser.add_argument('--quiet-minutes', type=float, default=120,
                        help='Treat a case as RECENT (untouchable) if CaseStatus/.git changed this recently (default 120)')
    parser.add_argument('--max-cases', type=int, default=0, help='run: pack at most N cases')
    parser.add_argument('--allow-unknown-jobs', action='store_true',
                        help='run: proceed although some Slurm jobs cannot be attributed to a case')
    parser.add_argument('--measure', action='store_true',
                        help='audit: also count entries in case dir, RUNDIR, EXEROOT(bld), archive (slower; read-only)')
    args = parser.parse_args(argv)
    args.name_re = re.compile(args.name_regex) if args.name_regex else None
    args.cohorts = args.cohort_list or ([] if (args.cases or args.name_re) else SCOPED)
    try:
        if args.mode == 'audit':
            root, rows, jobs, unknown, queue_ok, queue_error, skipped = survey(args)
            print_report(root, rows, jobs, unknown, queue_ok, queue_error, skipped, args)
            return 0
        return run(args)
    except (OSError, RuntimeError) as exc:
        print('STOP: {}'.format(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
