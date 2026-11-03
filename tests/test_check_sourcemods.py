"""check_sourcemods.py on a fake case tree: report is read-only, --fix relinks only dangling links."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOL = Path(__file__).resolve().parents[1] / 'scripts/check_sourcemods.py'


class CheckSourceModsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.R = Path(os.path.realpath(self.tmp.name))
        self.CR = self.R / 'cases'
        (self.R / 'home').mkdir()
        (self.R / 'bin').mkdir()
        owner_bld = self.R / 'OUT/OWNER/bld'
        owner_bld.mkdir(parents=True)
        (owner_bld / 'cesm.exe').write_bytes(b'E')
        self.case('OWNER', '$CIME_OUTPUT_ROOT/OWNER/bld')
        (self.CR / 'OWNER/SourceMods/src.drv').mkdir(parents=True)
        self.case('CLONE_DANGLING', '$CIME_OUTPUT_ROOT/OWNER/bld')
        os.symlink(str(self.CR / 'DELETED_SOURCE/SourceMods'), str(self.CR / 'CLONE_DANGLING/SourceMods'))
        self.case('CLONE_OK', '$CIME_OUTPUT_ROOT/OWNER/bld')
        os.symlink(str(self.CR / 'OWNER/SourceMods'), str(self.CR / 'CLONE_OK/SourceMods'))
        self.case('OWN_BUILD_DANGLING', str(self.R / 'OUT/OWN_BUILD_DANGLING/bld'))
        os.symlink(str(self.CR / 'GONE/SourceMods'), str(self.CR / 'OWN_BUILD_DANGLING/SourceMods'))
        (self.R / 'bin/squeue').write_text('#!/bin/sh\necho "11|PENDING|run.x|%s"\n' % (self.CR / 'CLONE_DANGLING'))
        (self.R / 'bin/squeue').chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.R / 'home'),
                        PATH=str(self.R / 'bin') + ':' + os.environ['PATH'])

    def tearDown(self):
        self.tmp.cleanup()

    def case(self, name, exeroot):
        c = self.CR / name
        c.mkdir(parents=True)
        (c / 'env_case.xml').write_text('<file><entry id="CIME_OUTPUT_ROOT" value="%s"/></file>' % (self.R / 'OUT'))
        (c / 'env_build.xml').write_text('<file><entry id="EXEROOT" value="%s"/></file>' % exeroot)

    def run_tool(self, *args):
        p = subprocess.run([sys.executable, '-B', str(TOOL), '--cases', str(self.CR)] + list(args),
                           env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stdout)
        return p.stdout

    def test_report_then_fix(self):
        out = self.run_tool()
        self.assertIn('READ ONLY', out)
        self.assertIn('CLONE_DANGLING\tDANGLING', out)
        self.assertIn('RELINK -> ' + str(self.CR / 'OWNER/SourceMods'), out)
        self.assertIn('OWN_BUILD_DANGLING', out)
        self.assertIn('NEED_RESTORE', out)
        self.assertIn('11 PENDING run.x', out)                 # active job in a broken case
        self.assertFalse((self.CR / 'CLONE_DANGLING/SourceMods').exists())

        out = self.run_tool('--fix', '--confirm', 'RELINK_SOURCEMODS')
        self.assertIn('RELINKED CLONE_DANGLING', out)
        self.assertTrue((self.CR / 'CLONE_DANGLING/SourceMods/src.drv').is_dir())
        self.assertEqual(os.readlink(str(self.CR / 'CLONE_OK/SourceMods')), str(self.CR / 'OWNER/SourceMods'))
        self.assertFalse((self.CR / 'OWN_BUILD_DANGLING/SourceMods').exists())  # not touched
        self.assertFalse((self.CR / 'DELETED_SOURCE').exists())                 # nothing recreated
        logs = list((self.R / 'home').glob('sourcemods_relink_*.tsv'))
        self.assertEqual(len(logs), 1)
        self.assertIn('CLONE_DANGLING', logs[0].read_text())

    def test_fix_needs_token(self):
        p = subprocess.run([sys.executable, '-B', str(TOOL), '--cases', str(self.CR), '--fix'],
                           env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertNotEqual(p.returncode, 0)


if __name__ == '__main__':
    unittest.main()
