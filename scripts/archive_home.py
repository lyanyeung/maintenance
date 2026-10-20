#!/usr/bin/env python3
"""IRIDIS6 HOME declutter: audit / verified Research Filestore snapshot / cautious cleanup.

No changes to Scratch, CIME cases, environments, or Github checkouts.
Python 3.9 compatible. Run on an IRIDIS login node.
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time

HOME = Path.home().resolve()
DEST = Path('/research/xclimatetbm/ly3n24/Home_Declutter')
# Exclude workspaces which may contain live configs, run scripts or program libraries.
PROTECTED = {
    'CARDAMOM', 'R', 'fcm', 'github', 'local', 'TCCAS-workflows',
    'maintenance', 'home_restore_review',
    'CLM-FATES-single-site-workflows',
    'CLM-FATES-single-site-workflows-analysis',
    'CLM_FATES_scripts', 'CLM_FATES_workflows_reference',
    'CLM_FATES_13_checkpoint',
    'FLUXNET_MultiSite_11', 'FI_Sod_workflow',
    'DE_Hai_recovery_20261020',
}
# Only leaf files directly in HOME can be auto-cleaned. These are just candidates;
# copied+verified, unchanged, sufficiently old, and no Slurm references are mandatory.
CLEAN_SUFFIXES = ('.log', '.out', '.err', '.bak', '.zip')
CLEAN_NAMING = re.compile(r'(_preview_run\.txt|\.before_[\w.\-]+)$', re.I)

def error(message):
    raise RuntimeError(message)

def human(n):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            return '%.1f %s' % (n, unit)
        n /= 1024

def sha_reader(f):
    h = hashlib.sha256()
    for b in iter(lambda: f.read(1024 * 1024 * 4), b''):
        h.update(b)
    return h.hexdigest()

def sha(path):
    with path.open('rb') as f:
        return sha_reader(f)

def kind_for(rel, is_dir):
    if is_dir:
        return 'BACKUP_ONLY_DIR'
    if rel.suffix.lower() in CLEAN_SUFFIXES or CLEAN_NAMING.search(rel.name):
        return 'CLEAN_CANDIDATE'
    return 'BACKUP_ONLY_FILE'

def selections():
    picked, excluded = [], []
    for p in sorted(HOME.iterdir(), key=lambda p: p.name):
        # No hidden program settings; do not preserve/alter existing repos and live projects.
        if p.name.startswith('.') or p.name in PROTECTED:
            excluded.append(p.name)
        elif p.is_socket() if hasattr(p, 'is_socket') else False:
            excluded.append(p.name)
        else:
            picked.append(p)
    return picked, excluded

def scan(paths):
    """Return deterministic tree manifest with original bytes and symlink targets.

    Deliberately does not follow symlinks or crawl into protected directory names.
    """
    data = {}
    def descend(p):
        rel = p.relative_to(HOME).as_posix()
        st = p.lstat()
        common = {'mtime_ns':st.st_mtime_ns, 'mode':stat.S_IMODE(st.st_mode)}
        if stat.S_ISLNK(st.st_mode):
            data[rel] = dict(common, kind='link', link=os.readlink(p))
        elif stat.S_ISREG(st.st_mode):
            data[rel] = dict(common, kind='file', size=st.st_size, sha256=sha(p))
        elif stat.S_ISDIR(st.st_mode):
            data[rel] = dict(common, kind='dir')
            for c in sorted(p.iterdir(), key=lambda x:x.name):
                descend(c)
        else:
            error('Special file detected, will not archive: ' + str(p))
    for p in paths:
        descend(p)
    return data

def archive_check(tar_path, snap):
    seen = set()
    with tarfile.open(tar_path, 'r:gz') as t:
        for m in t:
            rel = m.name.removeprefix('home/')
            if not m.name.startswith('home/') or rel not in snap or rel in seen:
                error('Unexpected or duplicate archive member: '+m.name)
            x = snap[rel]
            if x['kind'] == 'file' and m.isfile() and m.size == x['size']:
                inp = t.extractfile(m)
                if inp is None:
                    error('Cannot read tar member: '+m.name)
                with inp:
                    if sha_reader(inp) != x['sha256']:
                        error('Byte mismatch for '+rel)
            elif x['kind'] == 'dir' and m.isdir():
                pass
            elif x['kind'] == 'link' and m.issym() and m.linkname == x['link']:
                pass
            else:
                error('Wrong file type or unexpected hard link in TAR: ' + m.name)
            seen.add(rel)
    if seen != set(snap):
        error('TAR missing entries: '+repr(list(set(snap)-seen)[:10]))

def storage_ok():
    # Trigger automount; then verify effective filesystem, not top-level autofs.
    if not DEST.parent.is_dir():
        error('Research Filestore unavailable: ' + str(DEST.parent))
    p = subprocess.run(['stat', '-f', '-c', '%T', str(DEST.parent)], capture_output=True, text=True)
    if p.returncode or p.stdout.strip() not in ('nfs', 'nfs4'):
        error('Research Filestore is not verified NFS: ' + repr(p.stdout.strip()))
    DEST.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(['stat', '-f', '-c', '%T', str(DEST)], capture_output=True, text=True)
    if p.returncode or p.stdout.strip() not in ('nfs', 'nfs4'):
        error('Destination is not verified NFS')

def audit():
    picks, excluded = selections()
    from collections import Counter
    groups = Counter(kind_for(p, p.is_dir()) for p in picks)
    print('HOME:', HOME)
    print('FILESTORE:', DEST)
    print('Root selections:', len(picks), ' / protected or hidden:', len(excluded))
    print('Classification:', dict(groups))
    print('\nTOP-LEVEL CLEANUP CANDIDATES (subset)')
    for p in picks:
        if kind_for(p, p.is_dir()) == 'CLEAN_CANDIDATE':
            print('  ', p.name)
    print('\nBACKUP-ONLY DIRECTORIES (cannot auto-delete):')
    for p in picks:
        if p.is_dir(): print('  ', p.name)
    print('\nPROTECTED TOP-LEVEL ENTRIES (excluded):')
    print('  ', ', '.join(excluded))
    print('\nNothing changed.')

def new_id():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')

def backup():
    storage_ok()
    picks, excluded = selections()
    if not picks: error('No candidate items found')
    stamp = new_id()
    session = DEST / ('home_snapshot_'+stamp)
    session.mkdir(exist_ok=False)
    tar = session/'home_selected.tar.gz'
    temp = session/'home_selected.tar.gz.partial'
    print('SNAPSHOT:',session,flush=True)
    print('Selected items:',len(picks), 'protected:',len(excluded),flush=True)
    initial = scan(picks)
    manifest = {
        'version':1, 'created_utc':stamp, 'home':str(HOME),
        'selected_top': [p.name for p in picks],
        'excluded_top':excluded, 'entries':initial,
        'tar_file':tar.name, 'verified':False,
    }
    with tarfile.open(temp, 'w:gz', dereference=False, format=tarfile.PAX_FORMAT) as t:
        for p in picks:
            t.add(p, arcname='home/'+p.name, recursive=True)
    print('Created TAR; now checking every archived member...',flush=True)
    archive_check(temp, initial)
    current = scan(picks)
    if current != initial:
        error('Home source changed while backup was running. NO DELETION. Leave session for inspection: '+str(session))
    manifest['sha256'] = sha(temp)
    manifest['verified'] = True
    (session/'manifest.json.partial').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
    temp.rename(tar)
    (session/'manifest.json.partial').rename(session/'manifest.json')
    (session/'SHA256SUMS').write_text(manifest['sha256']+'  '+tar.name+'\n')
    print('VERIFIED_BACKUP',session,flush=True)
    print('Archive size:',human(tar.stat().st_size),'total files:',sum(1 for v in initial.values() if v['kind']=='file'),flush=True)
    print('To clean only verified OLD root log/zip/backup files:')
    print('python3 '+str(Path(__file__).resolve())+' clean --session '+str(session)+' --confirm DELETE_OLD_VERIFIED_FILES')

def verify_session(session):
    storage_ok()
    session=session.resolve(strict=True)
    try:session.relative_to(DEST.resolve())
    except ValueError:error('Archive session not within expected Research Filestore')
    m=json.loads((session/'manifest.json').read_text())
    if not m.get('verified') or m.get('version')!=1 or m.get('home')!=str(HOME):
        error('Manifest not a verified backup of this HOME')
    tar=session/m['tar_file']
    if not tar.is_file() or sha(tar)!=m['sha256']:
        error('TAR SHA-256 mismatch')
    if (session/'SHA256SUMS').read_text().split()[0]!=m['sha256']:
        error('SHA256 receipt mismatch')
    print('Archive SHA256 verified, checking members...',flush=True)
    archive_check(tar,m['entries'])
    print('VERIFIED_SESSION',session,flush=True)
    return m

def queued_slurm():
    user=os.environ.get('USER',HOME.name)
    p=subprocess.run(['squeue','-h','-u',user,'-o','%i'],capture_output=True,text=True)
    if p.returncode:
        error('Unable to inspect active Slurm jobs; no deletion permitted: '+p.stderr)
    refs=[]
    for job in p.stdout.split():
        q=subprocess.run(['scontrol','show','job','-o',job],capture_output=True,text=True)
        if q.returncode:
            error('Unable to inspect job '+job+'; no deletion permitted')
        refs.append((job,q.stdout))
    return refs

def clean(session,confirm,min_age_hours):
    if confirm!='DELETE_OLD_VERIFIED_FILES':
        error('Cleaning requires --confirm DELETE_OLD_VERIFIED_FILES')
    m=verify_session(session)
    jobs=queued_slurm()
    candidates=[]; skipped=[]
    threshold=time.time() - min_age_hours*3600
    for name in m['selected_top']:
        p=HOME/name
        entry=m['entries'].get(name)
        if not entry or entry['kind']!='file' or kind_for(p,False)!='CLEAN_CANDIDATE':
            continue
        if not p.is_file() or p.is_symlink():
            skipped.append((name,'missing or not regular'));continue
        st=p.stat()
        if st.st_mtime > threshold:
            skipped.append((name,'too recent'));continue
        if any(str(p) in details for job,details in jobs):
            skipped.append((name,'Slurm job references path'));continue
        if st.st_size!=entry['size'] or st.st_mtime_ns!=entry['mtime_ns'] or sha(p)!=entry['sha256']:
            skipped.append((name,'changed from verified backup'));continue
        candidates.append(p)
    # Recheck the Slurm queue before beginning destructive actions.
    queued_slurm()
    receipt=session/'cleanup_actions.tsv'
    if receipt.exists():
        error('Cleanup receipt already exists; refuse accidental second pass')
    print('Ready to remove',len(candidates),'verified old root files; skipping',len(skipped))
    with receipt.open('x') as f:
        f.write('path\tsha256\tbytes\tstatus\n');f.flush()
        os.fsync(f.fileno())
        for p in candidates:
            entry=m['entries'][p.name]
            try:
                st=p.stat()
                if st.st_mtime > threshold or st.st_size!=entry['size'] or st.st_mtime_ns!=entry['mtime_ns'] or sha(p)!=entry['sha256']:
                    skipped.append((p.name,'changed on final recheck'));continue
                # Single verified leaf file; never remove a directory or symlink.
                p.unlink()
                f.write('%s\t%s\t%d\tREMOVED\n'%(p.name,entry['sha256'],entry['size']))
                f.flush();os.fsync(f.fileno())
                print('REMOVED',p.name,flush=True)
            except OSError as e:
                skipped.append((p.name,'unlink error: '+str(e)))
        for name,why in skipped:
            f.write('%s\t\t\tSKIPPED: %s\n'%(name,why))
    print('CLEANUP_RECEIPT',receipt,flush=True)
    print('Only top-level old logs/zip/backup files were eligible. Scripts, CSV, all directories kept.')

def main():
    a=argparse.ArgumentParser(description=__doc__)
    a.add_argument('action',choices=('audit','backup','verify','clean'))
    a.add_argument('--session',type=Path)
    a.add_argument('--confirm',default='')
    a.add_argument('--min-age-hours',type=float,default=48)
    v=a.parse_args()
    if v.min_age_hours<24:
        error('For safety, minimum age must be at least 24 hours')
    if v.action=='audit':audit()
    elif v.action=='backup':backup()
    else:
        if not v.session: error('--session is required for verify/clean')
        if v.action=='verify':verify_session(v.session)
        elif v.action=='clean':clean(v.session,v.confirm,v.min_age_hours)
if __name__=='__main__':
    try:main()
    except (RuntimeError,OSError,tarfile.TarError,ValueError) as e:
        print('STOP:',e,file=sys.stderr);sys.exit(2)