"""End-to-end test of offload_legacy_spinup1000.py on a fake Scratch tree (no Slurm/NFS needed)."""
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

TOOL = Path(__file__).resolve().parents[1] / 'scripts/offload_legacy_spinup1000.py'


class LegacyOffloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.R = Path(self.tmp.name)
        self.S = self.R / 'scratch/ly3n24'
        self.CR, self.OUT = self.S / 'cases', self.S / 'OUT'
        for p in (self.CR, self.OUT / 'archive', self.R / 'home', self.R / 'research', self.R / 'bin'):
            p.mkdir(parents=True)
        self.mk('DK-Sor_FATES_SPINUP500', 1996, 3100, 3101)          # batch-6 continuation
        self.mk('BE-Bra_FATES_SPINUP500_11P', 1996, 2995, 2996)       # complete 1000
        self.mk('CH-Aws_FATES_SPINUP1000_RH34A', 2006, 2015, 2016)
        self.mk('CH-Aws_FATES_SPINUP1000_RH42A_V8_CONT2016', 2006, 2020, 2021,
                text='RUN_REFCASE CH-Aws_FATES_SPINUP1000_RH34A')
        (self.OUT / 'SHARED/bld').mkdir(parents=True)
        self.mk('FI-Sod_B01_FIXNC_AD400', 2001, 2005, 2006, exe=str(self.OUT / 'SHARED/bld'))
        old = time.time() - 86400
        for root, ds, fs in os.walk(str(self.R)):
            for x in ds + fs:
                os.utime(os.path.join(root, x), (old, old), follow_symlinks=False)
        sq = self.R / 'bin/squeue'
        sq.write_text('#!/bin/sh\necho "7|run.CH-Aws_FATES_SPINUP1000_RH42A_V8_CONT2016|RUNNING|/x|y"\n')
        sq.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.R / 'home'), PATH=str(self.R / 'bin') + ':' + os.environ['PATH'])

    def tearDown(self):
        self.tmp.cleanup()

    def mk(self, name, start, h0a_last, rest_last, text='', exe=None):
        c = self.CR / name
        c.mkdir()
        rd, ar = self.OUT / name / 'run', self.OUT / 'archive' / name
        kv = dict(RUNDIR=rd, EXEROOT=exe or self.OUT / name / 'bld', DOUT_S_ROOT=ar,
                  RUN_STARTDATE='%d-01-01' % start, RESUBMIT=0)
        (c / 'env_run.xml').write_text('<file>' + ''.join(
            '<entry id="%s" value="%s"/>' % kv_ for kv_ in kv.items()) + '</file>')
        (c / 'CaseStatus').write_text('2026-10-20 10:00:00: case.run success\n'
                                      '2026-10-20 11:00:00: st_archive success\n')
        (c / 'user_nl_clm').write_text(text)
        rd.mkdir(parents=True)
        (rd / 'cesm.log').write_text('x')
        if exe is None:
            (self.OUT / name / 'bld').mkdir()
            (self.OUT / name / 'bld/cesm.exe').write_bytes(b'E')
        (ar / 'lnd/hist').mkdir(parents=True)
        for y in range(start, h0a_last + 1):
            (ar / 'lnd/hist' / ('%s.clm2.h0a.%04d-01-01-00000.nc' % (name, y))).write_bytes(b'h')
        for y in range(start + 5, rest_last + 1, 5):
            d = ar / 'rest' / ('%04d-01-01-00000' % y)
            d.mkdir(parents=True)
            (d / ('%s.clm2.r.%04d-01-01-00000.nc' % (name, y))).write_bytes(b'r')

    def run_tool(self, *args):
        common = ['--scratch-root', str(self.S)]
        if args[0] == 'plan':
            common += ['--case-root', str(self.CR), '--out-root', str(self.OUT)]
        else:
            common += ['--case-root', str(self.CR), '--allow-non-nfs-dest']
        p = subprocess.run([sys.executable, '-B', str(TOOL)] + list(args) + common, env=self.env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stdout)
        return p.stdout

    def test_end_to_end(self):
        out = self.run_tool('plan')
        self.assertIn('HELD\tCH-Aws_FATES_SPINUP1000_RH34A', out)          # held by the active branch
        self.assertIn('HELD\tCH-Aws_FATES_SPINUP1000_RH42A_V8_CONT2016\tACTIVE', out)
        self.assertNotIn('FI-Sod_B01_FIXNC_AD400', out)
        plan = sorted((self.R / 'home/offload_plans').glob('*.json'))[-1]
        dest = self.R / 'research/Scratch_Offload'
        out = self.run_tool('archive', '--plan', str(plan), '--dest', str(dest))
        self.assertIn('verified=2 skipped=0', out)
        session = next(dest.glob('legacy1000_*'))
        names = tarfile.open(str(session / 'DK-Sor_FATES_SPINUP500.tar')).getnames()
        h0a = sorted(n for n in names if '.h0a.' in n)
        self.assertEqual(len(h0a), 1000)
        self.assertIn('h0a.2995-', h0a[-1])
        self.assertFalse(any('/rest/3001-' in n for n in names))
        self.assertTrue(any('/rest/2996-' in n for n in names))
        self.assertFalse(any('/run/' in n for n in names))
        self.run_tool('verify', '--session', str(session))
        out = self.run_tool('clean', '--session', str(session), '--confirm', 'DELETE_VERIFIED_LEGACY_SPINUP1000')
        self.assertIn('kept_changed=0', out)
        for gone in ('cases/DK-Sor_FATES_SPINUP500', 'OUT/DK-Sor_FATES_SPINUP500',
                     'OUT/archive/DK-Sor_FATES_SPINUP500', 'cases/BE-Bra_FATES_SPINUP500_11P'):
            self.assertFalse((self.S / gone).exists(), gone)
        for kept in ('cases/FI-Sod_B01_FIXNC_AD400', 'OUT/SHARED/bld', 'cases/CH-Aws_FATES_SPINUP1000_RH34A',
                     'OUT/archive/CH-Aws_FATES_SPINUP1000_RH34A'):
            self.assertTrue((self.S / kept).exists(), kept)

    def test_sourcemods_link_holds_clone_source(self):
        """create_clone --keepexe: <clone>/SourceMods -> <source>/SourceMods. The source case
        must be held, or every clone's case.run fails (user_xml_dir .../src.drv missing)."""
        self.mk('FI-Sod_FATES_SPINUP500', 2001, 2010, 2011)
        src = self.CR / 'FI-Sod_FATES_SPINUP500/SourceMods/src.drv'
        src.mkdir(parents=True)
        os.symlink(str(src.parent), str(self.CR / 'FI-Sod_B01_FIXNC_AD400/SourceMods'))
        old = time.time() - 86400
        for root, ds, fs in os.walk(str(self.R)):
            for x in ds + fs:
                os.utime(os.path.join(root, x), (old, old), follow_symlinks=False)
        out = self.run_tool('plan')
        line = [l for l in out.splitlines() if l.startswith('HELD\tFI-Sod_FATES_SPINUP500\t')]
        self.assertTrue(line, out)
        self.assertIn('case-folder symlink of FI-Sod_B01_FIXNC_AD400', line[0])

    def test_symlink_hops_see_intermediate_case(self):
        """C -> B/SourceMods -> A/SourceMods: deleting B also breaks C, so B is a hop."""
        sys.path.insert(0, str(TOOL.parent))
        import importlib.util
        spec = importlib.util.spec_from_file_location('ofc', str(TOOL.parent / 'offload_failed_cases.py'))
        O = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(O)
        for n in 'ABC':
            (self.CR / n).mkdir()
        (self.CR / 'A/SourceMods').mkdir()
        os.symlink(str(self.CR / 'A/SourceMods'), str(self.CR / 'B/SourceMods'))
        os.symlink('../B/SourceMods', str(self.CR / 'C/SourceMods'))
        hops = O.case_symlink_hops(self.CR / 'C')
        real = Path(os.path.realpath(str(self.CR)))
        self.assertEqual(hops, [real / 'B/SourceMods', real / 'A/SourceMods'])


if __name__ == '__main__':
    unittest.main()
