"""Deletion-boundary and restore-integrity regression tests; no Slurm/NFS needed."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import time
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('cleanup', Path(__file__).resolve().parents[1] /
                                            'scripts/scratch_cache_log_cleanup.py')
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.scratch = self.base / 'scratch'
        self.home = self.base / 'home'
        self.backup = self.base / 'off_scratch_backup'
        for p in (self.scratch, self.home, self.backup):
            p.mkdir()
        self.site = self.scratch / 'CLM_FATES/sites/MY_EC_SITE/US-Test'
        self.subset = self.site / 'subset_data'
        self.cache = self.write(self.subset / 'ERA5/clmforc.ERA5.TBOT.2001-01.nc', b'cache')
        self.surf = self.write(self.subset / 'surfdata_US-Test.nc', b'CDF\x02surface')
        self.config = self.write(self.scratch / 'CLM_FATES/cases/US-Test_CASE/CaseDocs/lnd_in',
                                 ('fsurdat = "' + str(self.surf) + '"\n').encode())
        self.log = self.write(self.home / 'old.log', b'old diagnostic\n')

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, data, age_days=10):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))
        return path

    def audit(self, jobs=()):
        with contextlib.redirect_stdout(io.StringIO()):
            return C.audit(self.scratch, self.home, 7, jobs)

    def row(self, path, jobs=()):
        return next(r for r in self.audit(jobs) if r['path'] == str(path))

    def archive(self, rows):
        with contextlib.redirect_stdout(io.StringIO()):
            return C.archive_rows(rows, self.backup)

    def prune(self, doc, session, rows, jobs=()):
        with contextlib.redirect_stdout(io.StringIO()):
            C.prune(doc, session, rows, jobs, self.scratch)

    def test_configured_surface_and_unreferenced_cache_eligible(self):
        self.assertTrue(self.row(self.cache)['eligible'])
        self.assertTrue(self.row(self.log)['eligible'])

    def test_other_batch_cache_needs_no_surface_gate(self):
        self.config.unlink()
        self.assertTrue(self.row(self.cache)['eligible'])

    def test_surface_is_never_modified_or_used_as_deletion_authority(self):
        self.surf.write_bytes(b'not a netcdf file')
        self.assertTrue(self.row(self.cache)['eligible'])
        self.assertNotIn(str(self.surf), {r['path'] for r in self.audit()})

    def test_recent_logs_kept(self):
        os.utime(self.log, None)
        self.assertFalse(self.row(self.log)['eligible'])

    def test_observations_derived_forcing_model_data_never_candidates(self):
        for rel in ('CLM_FATES/sites/MY_EC_SITE/US-Test/forcing/raw/AMF_US-Test_FLUXMET_HH.csv',
                    'CLM_FATES/sites/MY_EC_SITE/US-Test/forcing/processed/ERA5_selected_forcing.nc',
                    'CLM_FATES/experiments/OtherBatch/science_trials/ERA5_trial.nc',
                    'CTSM_FATES_OUTPUTS/US-Test_CASE/run/cesm.log',
                    'CTSM_FATES_OUTPUTS/archive/US-Test_CASE/rest/2001/restart.nc',
                    'CLM_FATES/cases/US-Test_CASE/.git/objects/aa/object.log'):
            self.write(self.scratch / rel, b'protected')
        paths = {r['path'] for r in self.audit()}
        self.assertEqual(paths, {str(self.cache), str(self.log)})

    def test_all_twenty_manifest_sites_explicitly_protected(self):
        self.assertEqual(len(C.PROTECTED_SITES), 20)
        for site in C.PROTECTED_SITES:
            path = self.write(self.scratch / ('CLM_FATES/sites/MY_EC_SITE/' + site + '/forcing/raw/AMF_' + site + '_FLUXNET_ERA5_HH.csv'), b'ERA')
            row = self.row(path)
            self.assertFalse(row['eligible'], site)
            self.assertIn('KEEP_AMERIFLUX_BATCH03_OR_BATCH05', row['reasons'])

    def test_both_batch_evidence_folders_protected_even_without_site_in_filename(self):
        for batch in ('AmeriFlux_MultiSite_10_Batch03', 'AmeriFlux_MultiSite_10_Batch05',
                      'AmeriFlux_Batch03_10', 'AmeriFlux_Batch05_10'):
            path = self.write(self.scratch / ('CLM_FATES/experiments/' + batch + '/external_evidence/era5land/result.nc'), b'ERA')
            self.assertFalse(self.row(path)['eligible'])

    def test_other_era_sources_eligible_but_receipts_and_mixed_packages_retained(self):
        root = self.scratch / 'CLM_FATES/experiments/AmeriFlux_MultiSite_10_Batch04/external_evidence/era5land'
        payloads = [self.write(root / name, b'ERA') for name in ('result.nc', 'timeseries.json', 'source.zip', 'hourly.csv')]
        protected = [self.write(root / name, b'keep') for name in ('receipt.json', 'request.json', 'source_SHA.json',
                     'AMF_US-Test_FLUXNET_2000-2020.zip', 'AMF_US-Test_FLUXMET_HH.csv')]
        rows = {r['path']: r for r in self.audit()}
        for path in payloads:
            self.assertTrue(rows[str(path)]['eligible'])
        for path in protected:
            self.assertNotIn(str(path), rows)

    def test_other_standalone_era5_companion_can_be_removed(self):
        path = self.write(self.site / 'forcing/raw/release/AMF_US-Test_FLUXNET_ERA5_HH_2000-2020.csv', b'ERA')
        self.assertTrue(self.row(path)['eligible'])

    def test_era_age_gate_removed_but_log_retention_remains(self):
        os.utime(self.cache, None)
        os.utime(self.log, None)
        self.assertTrue(self.row(self.cache)['eligible'])
        self.assertFalse(self.row(self.log)['eligible'])

    def test_historical_other_batch_receipt_does_not_block_authorised_era_removal(self):
        self.write(self.scratch / 'CLM_FATES/experiments/OtherBatch/old_receipt.json', str(self.cache).encode())
        row = self.row(self.cache)
        self.assertTrue(row['eligible'])
        self.assertIsNotNone(row['historical_reference_restore_if_needed'])

    def test_cross_batch_shared_era_referenced_from_protected_batch_kept(self):
        self.write(self.scratch / 'CLM_FATES/experiments/AmeriFlux_MultiSite_10_Batch03/receipt.json', str(self.cache).encode())
        self.assertFalse(self.row(self.cache)['eligible'])

    def test_transitive_shared_dependency_from_protected_receipt_kept(self):
        shared = self.write(self.scratch / 'CLM_FATES/experiments/OtherBatch/shared_receipt.json', str(self.cache).encode())
        self.write(self.scratch / 'CLM_FATES/experiments/AmeriFlux_MultiSite_10_Batch05/receipt.json', str(shared).encode())
        self.assertFalse(self.row(self.cache)['eligible'])

    def test_missing_referenced_metadata_does_not_break_protection_audit(self):
        self.write(self.scratch / 'CLM_FATES/experiments/AmeriFlux_MultiSite_10_Batch05/receipt.json',
                   b'{"old_receipt":"/nonexistent/prior_receipt.json"}')
        self.assertTrue(self.row(self.cache)['eligible'])

    def test_protected_batch_symlink_to_other_batch_cache_kept(self):
        link = self.scratch / 'CLM_FATES/sites/MY_EC_SITE/US-Me6/forcing/raw/shared'
        link.parent.mkdir(parents=True)
        link.symlink_to(self.cache.parent, target_is_directory=True)
        self.assertFalse(self.row(self.cache)['eligible'])

    def test_protected_site_in_other_cache_filename_kept(self):
        path = self.write(self.scratch / 'CLM_FATES/experiments/OtherBatch/external_evidence/ERA5_US-Wkg.nc', b'ERA')
        self.assertFalse(self.row(path)['eligible'])

    def test_same_basename_is_not_proof_of_cross_batch_sharing(self):
        keep = self.write(self.scratch / 'CLM_FATES/experiments/AmeriFlux_MultiSite_10_Batch05/external_evidence/era5land/result.nc', b'keep')
        other = self.write(self.scratch / 'CLM_FATES/experiments/OtherBatch/external_evidence/era5land/result.nc', b'other')
        self.write(keep.parent / 'receipt.json', ('{"source":"' + str(keep) + '","result.nc":"sha"}').encode())
        self.assertFalse(self.row(keep)['eligible'])
        self.assertTrue(self.row(other)['eligible'])

    def test_legacy_v1_archives_remain_verifiable(self):
        session, doc = self.archive([self.row(self.log)])
        doc['version'] = '20261027-v1'
        (session / 'manifest.json').write_text(json.dumps(doc))
        self.assertEqual(C.verify_session(session)['version'], '20261027-v1')

    def test_surface_and_domain_files_never_candidates(self):
        self.write(self.subset / 'ERA5/domain.nc', b'CDFdomain')
        self.write(self.subset / 'ERA5/surfdata_ERA5.nc', b'CDFsurface')
        paths = {r['path'] for r in self.audit()}
        self.assertEqual(paths, {str(self.cache), str(self.log)})

    def test_same_site_queued_job_protects_cache(self):
        jobs = [dict(JobName='run.US-Test_CASE', WorkDir=str(self.scratch / 'CLM_FATES/cases/US-Test_CASE'))]
        self.assertFalse(self.row(self.cache, jobs)['eligible'])

    def test_active_job_stdout_protected(self):
        jobs = [dict(JobName='analysis', WorkDir=str(self.base / 'elsewhere'), StdOut=str(self.log))]
        self.assertFalse(self.row(self.log, jobs)['eligible'])

    def test_active_archived_case_logs_protected(self):
        log = self.write(self.scratch / 'CTSM_FATES_OUTPUTS/archive/US-Test_CASE/logs/cesm.log.gz', b'log')
        jobs = [dict(JobName='st_archive.US-Test_CASE', WorkDir=str(self.scratch / 'CLM_FATES/cases/US-Test_CASE'))]
        self.assertFalse(self.row(log, jobs)['eligible'])

    def test_exact_stream_reference_protects_cache(self):
        self.write(self.config.parent / 'datm.streams.txt', str(self.cache).encode())
        self.assertFalse(self.row(self.cache)['eligible'])

    def test_stream_directory_reference_protects_templates(self):
        self.write(self.config.parent / 'datm.streams.txt', (str(self.subset) + '\nclmforc.ERA5.%ym.nc').encode())
        self.assertFalse(self.row(self.cache)['eligible'])

    def test_receipt_relative_basename_protects_log(self):
        self.write(self.scratch / 'CLM_FATES/experiments/test/receipt.json', b'{"old.log":"source-sha"}')
        self.assertFalse(self.row(self.log)['eligible'])

    def test_relative_log_path_in_manifest_or_source_is_protected(self):
        self.write(self.scratch / 'CLM_FATES/experiments/test/input_manifest.csv', b'path\nlogs/old.log\n')
        self.assertFalse(self.row(self.log)['eligible'])
        (self.scratch / 'CLM_FATES/experiments/test/input_manifest.csv').unlink()
        self.write(self.home / 'ameriflux_test/source.py', b'LOG="./old.log"\n')
        self.assertFalse(self.row(self.log)['eligible'])

    def test_bare_archive_job_in_output_workdir_protects_site(self):
        jobs = [dict(JobName='st_archive', WorkDir=str(self.scratch / 'CTSM_FATES_OUTPUTS/US-Test_CASE/run'))]
        self.assertFalse(self.row(self.cache, jobs)['eligible'])

    def test_nfs_mount_failure_prevents_backup_directory_creation(self):
        target = self.backup / 'session_root'
        result = json.dumps({'filesystems':[{'target':'/', 'source':'/dev/root', 'fstype':'ext4'}]})
        with mock.patch.object(C, 'command', return_value=result):
            with self.assertRaises(RuntimeError):
                C.nfs_backup(target, self.scratch, self.home)
        self.assertFalse(target.exists())

    def test_mount_probe_handles_autofs_and_nfs_at_same_target(self):
        result = json.dumps({'filesystems':[
            {'target':str(self.backup), 'source':'automount', 'fstype':'autofs'},
            {'target':str(self.backup), 'source':'rfs:/research', 'fstype':'nfs4'}]})
        with mock.patch.object(C, 'command', return_value=result):
            info = C.mount_info(self.backup)
        self.assertEqual(info['fstype'], 'nfs4')
        self.assertEqual(info['source'], 'rfs:/research')

    def test_non_nfs_type_is_reported_and_still_requires_review(self):
        result = json.dumps({'filesystems':[{'target':str(self.backup),
                           'source':'//rfs/research', 'fstype':'cifs'}]})
        with mock.patch.object(C, 'command', return_value=result):
            info = C.mount_info(self.backup)
        with self.assertRaisesRegex(RuntimeError, 'cifs'):
            C.require_nfs(info)

    def test_ambiguous_mounts_fail_closed(self):
        result = json.dumps({'filesystems':[
            {'target':str(self.backup), 'source':'rfs:/a', 'fstype':'nfs4'},
            {'target':str(self.backup), 'source':'rfs:/b', 'fstype':'nfs4'}]})
        with mock.patch.object(C, 'command', return_value=result):
            with self.assertRaises(RuntimeError):
                C.mount_info(self.backup)

    def test_mount_probe_has_no_file_or_slurm_writes(self):
        output = io.StringIO()
        result = json.dumps({'filesystems':[{'target':str(self.backup),
                           'source':'rfs:/research', 'fstype':'nfs4'}]})
        before = list(self.backup.iterdir())
        with mock.patch.object(C, 'command', return_value=result), contextlib.redirect_stdout(output):
            C.probe_backup(self.backup / 'not-created')
        self.assertEqual(list(self.backup.iterdir()), before)
        self.assertIn('BACKUP_MOUNT_OBSERVED', output.getvalue())

    def test_queue_reads_full_pending_job_metadata(self):
        calls = ['4748798\n', 'JobId=4748798 JobName=run.US-Test_CASE JobState=PENDING WorkDir=/case StdOut=/old.log StdErr=/old.err Command=/case/.case.run\n']
        with mock.patch.object(C, 'command', side_effect=calls):
            jobs = C.queue()
        self.assertEqual(jobs[0]['JobName'], 'run.US-Test_CASE')
        self.assertEqual(jobs[0]['StdOut'], '/old.log')

    def test_queue_failure_is_not_treated_as_idle(self):
        with mock.patch.object(C, 'command', side_effect=RuntimeError('Slurm unavailable')):
            with self.assertRaises(RuntimeError):
                C.queue()

    def test_symlink_file_and_ancestor_not_followed(self):
        link = self.home / 'linked.log'
        link.symlink_to(self.log)
        self.assertIsNone(C.ordinary(link))
        (self.home / 'ameriflux_link').symlink_to(self.base, target_is_directory=True)
        self.assertIsNone(C.ordinary(self.home / 'ameriflux_link/home/old.log'))
        self.assertNotIn(str(link), {r['path'] for r in self.audit()})

    def test_hardlinked_files_not_eligible(self):
        os.link(self.log, self.home / 'other.log')
        self.assertIsNone(C.ordinary(self.log))

    def test_verified_archive_then_delete_and_restore_exact_content(self):
        row = self.row(self.log)
        session, doc = self.archive([row])
        self.assertTrue(self.log.exists())
        self.prune(C.verify_session(session), session, self.audit())
        self.assertFalse(self.log.exists())
        with tarfile.open(session / 'files.tar.gz', 'r:gz') as tar:
            self.assertEqual(tar.extractfile(doc['files'][0]['member']).read(), b'old diagnostic\n')
        self.assertTrue(self.cache.exists())
        self.assertTrue(self.surf.exists())

    def test_modified_source_retained_after_backup(self):
        session, doc = self.archive([self.row(self.log)])
        self.log.write_bytes(b'new content')
        self.prune(doc, session, self.audit())
        self.assertTrue(self.log.exists())

    def test_new_reference_or_active_job_after_backup_retains_source(self):
        session, doc = self.archive([self.row(self.cache)])
        self.write(self.config.parent / 'datm.streams.txt', str(self.cache).encode())
        self.prune(doc, session, self.audit())
        self.assertTrue(self.cache.exists())
        session2, doc2 = self.archive([self.row(self.log)])
        jobs = [dict(JobName='analysis', WorkDir=str(self.base / 'elsewhere'), StdOut=str(self.log))]
        self.prune(doc2, session2, self.audit(), jobs)
        self.assertTrue(self.log.exists())

    def test_corrupt_backup_fails_verification_before_delete(self):
        session, doc = self.archive([self.row(self.log)])
        with (session / 'files.tar.gz').open('ab') as f:
            f.write(b'corruption')
        with self.assertRaises(RuntimeError):
            C.verify_session(session)
        self.assertTrue(self.log.exists())

    def test_member_hash_mismatch_refuses_restore_receipt(self):
        session, doc = self.archive([self.row(self.log)])
        doc['files'][0]['sha256'] = '0' * 64
        (session / 'manifest.json').write_text(json.dumps(doc))
        with self.assertRaises(RuntimeError):
            C.verify_session(session)
        self.assertTrue(self.log.exists())


if __name__ == '__main__':
    unittest.main()
