#!/usr/bin/env python3
"""Find (and optionally repair) dangling SourceMods links in CIME case folders.

Background: `create_clone --keepexe` makes <clone>/SourceMods a symlink to the
SOURCE case's SourceMods. If that source case is later deleted (e.g. by an
offload/cleanup), every clone's SourceMods dangles, and case.run fails in
2-3 s with:  ERROR: user_xml_dir <case>/SourceMods/src.drv does not exist

The stable target is the SourceMods of the case that BUILT the executable
(cases/<EXEROOT parent>/SourceMods), the same rule used in
Non-Competition-CLM-experiment Batch02_10/44_* and Cross_cohort_NORMAL400/01_*.

Incident record: docs/SOURCEMODS_DANGLING_LINKS.md

Modes
  (default)  READ ONLY report: one row per case + summary + active Slurm jobs hit.
  --fix --confirm RELINK_SOURCEMODS
             Relink ONLY cases whose SourceMods is a dangling symlink AND whose
             executable-owner SourceMods resolves to a real directory that contains
             src.drv. Nothing is deleted except the dangling link itself.
             Every change is appended to ~/sourcemods_relink_<UTC>.tsv (Home).

Python 3.6+, standard library only.
"""
import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

VERSION = '20261103-v1'
TOKEN = 'RELINK_SOURCEMODS'
ENTRY_RE = re.compile(r'<entry\s+id="([A-Z0-9_]+)"\s+value="([^"]*)"')
VAR_RE = re.compile(r'\$\{?([A-Z0-9_]+)\}?')


def env_vars(case):
    """All <entry id=.. value=..> values from the case's env_*.xml (no xmlquery: fast)."""
    vals = {}
    for f in sorted(case.glob('env_*.xml')):
        try:
            for k, v in ENTRY_RE.findall(f.read_text(errors='replace')):
                vals.setdefault(k, v)
        except OSError:
            pass
    vals.setdefault('CASE', case.name)
    vals.setdefault('CASEROOT', str(case))
    return vals


def expand(value, vals, depth=0):
    if depth > 10 or '$' not in value:
        return value
    out = VAR_RE.sub(lambda m: vals.get(m.group(1), m.group(0)), value)
    return out if out == value else expand(out, vals, depth + 1)


def sm_state(sm):
    if sm.is_symlink():
        return 'link_ok' if sm.exists() else 'DANGLING'
    if sm.is_dir():
        return 'real_dir'
    if sm.exists():
        return 'NOT_A_DIR'
    return 'MISSING'


def active_jobs():
    """{case_name: [jobid state jobname]} for queued/running jobs whose WorkDir is a case."""
    try:
        out = subprocess.run(['squeue', '-u', os.environ.get('USER', ''), '-h',
                              '-o', '%i|%T|%j|%Z'], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, check=False).stdout.decode()
    except OSError:
        return {}
    jobs = {}
    for line in out.splitlines():
        parts = line.split('|')
        if len(parts) == 4:
            jobs.setdefault(Path(parts[3]).name, []).append(' '.join(parts[:3]))
    return jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cases', default='/iridisfs/scratch/ly3n24/CLM_FATES/cases')
    ap.add_argument('--fix', action='store_true')
    ap.add_argument('--confirm', default='')
    ap.add_argument('--case', action='append', default=[], help='limit to these case names')
    a = ap.parse_args()
    if a.fix and a.confirm != TOKEN:
        sys.exit('--fix needs --confirm ' + TOKEN)

    root = Path(a.cases)
    cases = sorted(p for p in root.iterdir()
                   if p.is_dir() and not p.is_symlink() and (p / 'env_case.xml').is_file())
    if a.case:
        cases = [c for c in cases if c.name in set(a.case)]
    jobs = active_jobs()

    rows, plan = [], []
    for case in cases:
        sm = case / 'SourceMods'
        state = sm_state(sm)
        target = os.readlink(str(sm)) if sm.is_symlink() else ''
        vals = env_vars(case)
        exeroot = expand(vals.get('EXEROOT', ''), vals)
        owner_name = Path(exeroot).parent.name if exeroot else ''
        owner_sm = root / owner_name / 'SourceMods' if owner_name else None
        owner_real = owner_sm.resolve() if owner_sm and owner_sm.exists() else None
        owner_ok = bool(owner_real and owner_real.is_dir() and (owner_real / 'src.drv').is_dir())
        exe_ok = 'exe_ok' if exeroot and Path(exeroot, 'cesm.exe').is_file() else 'EXE_MISSING'

        if state == 'DANGLING':
            if owner_name == case.name:
                action = 'NEED_RESTORE (own build; restore SourceMods from the offload tar)'
            elif owner_ok:
                action = 'RELINK -> ' + str(owner_real)
                plan.append((case, sm, target, owner_real))
            else:
                action = 'NEED_REVIEW (exe-owner SourceMods missing/dangling: {})'.format(owner_sm)
        elif state in ('NOT_A_DIR', 'MISSING'):
            action = 'NEED_REVIEW'
        elif not (sm / 'src.drv').is_dir():
            action = 'NO_SRC_DRV (mkdir -p {}/src.drv is enough)'.format(sm)
        else:
            action = 'ok'
        patched = (sm / 'src.clm' / 'EDPhysiologyMod.F90').is_file() if sm.exists() else False
        rows.append((case.name, state, target, owner_name, exe_ok, 'phenpatch' if patched else '-',
                     '; '.join(jobs.get(case.name, [])) or '-', action))

    print('case\tSourceMods\tlink_target\texe_owner\texe\tsrc.clm_patch\tactive_jobs\taction')
    for r in rows:
        print('\t'.join(r))

    print('\n== SUMMARY ==')
    for k, n in Counter(r[1] for r in rows).most_common():
        print('  {:10s} {}'.format(k, n))
    dang = Counter(r[2] for r in rows if r[1] == 'DANGLING')
    if dang:
        print('\n== dangling link targets (deleted folders) ==')
        for t, n in dang.most_common():
            print('  {:4d}  {}'.format(n, t))
    hit = [r for r in rows if r[7] != 'ok' and r[6] != '-']
    if hit:
        print('\n== ACTIVE/QUEUED jobs in broken cases (each will fail at its next case.run) ==')
        for r in hit:
            print('  {}  {}'.format(r[0], r[6]))
    print('\nrelinkable: {}   need restore/review: {}'.format(
        len(plan), sum(1 for r in rows if r[7].startswith('NEED'))))
    print('exe missing: {}'.format(sum(1 for r in rows if r[4] == 'EXE_MISSING')))

    if not a.fix:
        print('READ ONLY. Nothing changed. ({})'.format(VERSION))
        return
    log = Path.home() / 'sourcemods_relink_{}.tsv'.format(
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    with log.open('w') as fh:
        fh.write('utc\tcase\told_target\tnew_target\n')
        for case, sm, old, new in plan:
            if not (sm.is_symlink() and not sm.exists()):
                print('SKIP (changed since scan): ' + case.name)
                continue
            sm.unlink()
            os.symlink(str(new), str(sm))
            assert (sm / 'src.drv').is_dir(), case.name
            fh.write('{}\t{}\t{}\t{}\n'.format(datetime.now(timezone.utc).isoformat(),
                                               case.name, old, new))
            print('RELINKED {}: {} -> {}'.format(case.name, old, new))
    print('\nLog: ' + str(log))


if __name__ == '__main__':
    main()
