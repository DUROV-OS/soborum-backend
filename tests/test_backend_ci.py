"""Adversarial tests for the installed CI boundary; no Docker or production writes."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'ci' / 'backend_deploy.py'
spec = importlib.util.spec_from_file_location('backend_ci_boundary', MODULE)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)
SHA = 'a' * 40


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.previous_umask = os.umask(0o077)
        os.umask(self.previous_umask)

    def tearDown(self):
        os.umask(self.previous_umask)

    def test_exact_command_is_accepted(self):
        self.assertEqual(ci.command('deploy ' + SHA), SHA)

    def test_shell_scp_sftp_arguments_and_injection_are_rejected(self):
        for value in ['', 'id', 'bash', 'scp -t /tmp/config', 'internal-sftp',
                      'deploy ' + SHA + '; id', 'deploy ' + SHA + '\n',
                      'deploy ' + SHA + ' extra', 'deploy  ' + SHA,
                      'deploy "' + SHA + '"', 'deploy ' + SHA.upper(),
                      'deploy ' + 'a' * 39, 'deploy ' + 'a' * 41]:
            with self.subTest(command=value), self.assertRaises(ci.Rejected):
                ci.command(value)

    def test_root_helper_rejects_cli_arguments_before_actions(self):
        with patch.object(ci.sys, 'argv', ['soborum-backend-deploy', SHA]), \
                patch.object(ci, 'deploy') as deploy, patch.object(ci.sys, 'stderr', io.StringIO()):
            self.assertEqual(ci.main(), 1)
            deploy.assert_not_called()

    def test_root_helper_rejects_extra_stdin_before_actions(self):
        if not hasattr(os, 'geteuid') or os.geteuid() != 0:
            self.skipTest('root-only boundary checked in isolated Linux test')
        with patch.object(ci.sys, 'argv', ['soborum-backend-deploy']), \
                patch.object(ci.sys, 'stdin', io.TextIOWrapper(io.BytesIO((SHA + '\nid\n').encode()))), \
                patch.object(ci, 'secure_path'), patch.object(ci, 'deploy') as deploy, \
                patch.object(ci.sys, 'stderr', io.StringIO()):
            self.assertEqual(ci.main(), 1)
            deploy.assert_not_called()

    def test_dispatcher_passes_no_arguments_or_user_environment_to_sudo(self):
        with patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': 'deploy ' + SHA, 'PYTHONPATH': '/evil'}), \
                patch.object(ci.sys, 'argv', ['durov-gh-backend']), \
                patch.object(ci.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as call:
            self.assertEqual(ci.dispatcher(), 0)
            args, kwargs = call.call_args
            self.assertEqual(args[0], ['/usr/bin/sudo', '-n', '/usr/local/sbin/soborum-backend-deploy'])
            self.assertEqual(kwargs['input'], (SHA + '\n').encode())
            self.assertNotIn('PYTHONPATH', kwargs['env'])

    def test_dirty_content_and_symlink_are_rejected(self):
        if os.name == 'nt':
            self.skipTest('Linux O_NOFOLLOW boundary checked in isolated Linux test')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'app').mkdir()
            path = root / 'app' / 'module.py'
            path.write_bytes(b'expected')
            files = {'app/module.py': (0o644, b'expected')}
            ci.verify_files(root, files)
            path.write_bytes(b'modified')
            with self.assertRaises(ci.Rejected):
                ci.verify_files(root, files)
            path.unlink()
            try:
                path.symlink_to('/etc/passwd')
            except OSError:
                return
            with self.assertRaises(ci.Rejected):
                ci.verify_files(root, files)

    def test_mutable_control_path_is_rejected(self):
        if os.name == 'nt':
            self.skipTest('Unix ownership and mode test')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'helper'
            path.write_text('test')
            path.chmod(0o777)
            with self.assertRaises(ci.Rejected):
                ci.secure_path(path)

    def test_concurrent_release_is_rejected(self):
        if os.name == 'nt':
            self.skipTest('Unix flock test')
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'lock'
            with ci.release_lock(lock):
                with self.assertRaises(ci.Rejected):
                    with ci.release_lock(lock):
                        self.fail('second release acquired the lock')

    def test_non_runtime_changes_do_not_force_restart(self):
        before = {'app/a.py': (0o644, b'a'), '.github/workflows/deploy.yml': (0o644, b'old')}
        after = dict(before, **{'.github/workflows/deploy.yml': (0o644, b'new'),
                                'scripts/ci/backend_deploy.py': (0o644, b'helper')})
        self.assertEqual(ci.manifest(before, runtime=True), ci.manifest(after, runtime=True))
        after['app/a.py'] = (0o644, b'b')
        self.assertNotEqual(ci.manifest(before, runtime=True), ci.manifest(after, runtime=True))

    def test_checks_must_be_successful_from_github_actions_for_exact_sha(self):
        good = {'name': 'checks / checks', 'head_sha': SHA, 'app': {'slug': 'github-actions'},
                'status': 'completed', 'conclusion': 'success'}
        for change in [{'conclusion': 'failure'}, {'head_sha': 'b' * 40},
                       {'app': {'slug': 'other'}}, {'status': 'in_progress'}]:
            value = dict(good, **change)
            response = io.BytesIO(json.dumps({'check_runs': [value]}).encode())
            with patch.object(ci.urllib.request, 'urlopen', return_value=response), self.assertRaises(ci.Rejected):
                ci.require_checks(SHA)
        with patch.object(ci.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps({'check_runs': [good]}).encode())):
            ci.require_checks(SHA)

    def deployment_fixture(self, directory):
        state = {'verified_revision': 'b' * 40, 'legacy_revision': 'b' * 40,
                 'active_revision': 'b' * 40, 'config_hashes': {}}
        (Path(directory) / 'state.json').write_text(json.dumps(state))
        return state

    def test_stale_revision_rejected_before_deployment_or_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            self.deployment_fixture(directory)
            with patch.object(ci, 'STATE', Path(directory)), patch.object(ci, 'secure_path'), \
                    patch.object(ci, 'fetch_main', return_value='c' * 40), \
                    patch.object(ci, 'require_checks') as checks, patch.object(ci, 'snapshot') as backup:
                if os.name == 'nt':
                    self.skipTest('Linux release lock test')
                with self.assertRaises(ci.Rejected):
                    ci.deploy(SHA)
                checks.assert_not_called(); backup.assert_not_called()

    def test_dirty_tree_blocks_release_without_overwriting_it(self):
        if os.name == 'nt':
            self.skipTest('Linux release lock test')
        with tempfile.TemporaryDirectory() as directory:
            self.deployment_fixture(directory)
            with patch.object(ci, 'STATE', Path(directory)), patch.object(ci, 'secure_path'), \
                    patch.object(ci, 'fetch_main', return_value=SHA), patch.object(ci, 'git'), \
                    patch.object(ci, 'require_checks'), \
                    patch.object(ci, 'verify_legacy', side_effect=ci.Rejected('dirty tree')), \
                    patch.object(ci, 'snapshot') as backup, patch.object(ci, 'compose') as compose:
                with self.assertRaises(ci.Rejected):
                    ci.deploy(SHA)
                backup.assert_not_called(); compose.assert_not_called()

    def test_unchanged_runtime_verifies_revision_without_restart(self):
        if os.name == 'nt':
            self.skipTest('Linux release lock test')
        with tempfile.TemporaryDirectory() as directory:
            self.deployment_fixture(directory)
            files = {'app/a.py': (0o644, b'current')}
            with patch.object(ci, 'STATE', Path(directory)), patch.object(ci, 'secure_path'), \
                    patch.object(ci, 'fetch_main', return_value=SHA), patch.object(ci, 'git'), \
                    patch.object(ci, 'require_checks'), patch.object(ci, 'verify_legacy'), \
                    patch.object(ci, 'verify_configs'), patch.object(ci, 'tree', return_value=files), \
                    patch.object(ci, 'ready') as ready, patch.object(ci, 'compose') as compose, \
                    patch.object(ci, 'snapshot') as backup:
                result = ci.deploy(SHA)
                self.assertEqual(result['verified_revision'], SHA)
                self.assertFalse(result['restarted'])
                ready.assert_called_once(); compose.assert_not_called(); backup.assert_not_called()

    def test_migration_delta_requires_review_before_backup_or_restart(self):
        if os.name == 'nt':
            self.skipTest('Linux release lock test')
        with tempfile.TemporaryDirectory() as directory:
            self.deployment_fixture(directory)
            before = {'app/a.py': (0o644, b'current')}
            after = dict(before, **{'alembic/versions/new.py': (0o644, b'ddl')})
            with patch.object(ci, 'STATE', Path(directory)), patch.object(ci, 'secure_path'), \
                    patch.object(ci, 'fetch_main', return_value=SHA), patch.object(ci, 'git'), \
                    patch.object(ci, 'require_checks'), patch.object(ci, 'verify_legacy'), \
                    patch.object(ci, 'verify_configs'), patch.object(ci, 'tree', side_effect=[before, after]), \
                    patch.object(ci, 'compose') as compose, patch.object(ci, 'snapshot') as backup:
                with self.assertRaisesRegex(ci.Rejected, 'migration'):
                    ci.deploy(SHA)
                compose.assert_not_called(); backup.assert_not_called()

    def test_readiness_failure_rolls_back_only_application(self):
        if os.name == 'nt':
            self.skipTest('Linux release lock test')
        with tempfile.TemporaryDirectory() as directory:
            self.deployment_fixture(directory)
            before = {'app/a.py': (0o644, b'before')}
            after = {'app/a.py': (0o644, b'after')}
            with patch.object(ci, 'STATE', Path(directory)), patch.object(ci, 'secure_path'), \
                    patch.object(ci, 'fetch_main', return_value=SHA), patch.object(ci, 'git'), \
                    patch.object(ci, 'require_checks'), patch.object(ci, 'verify_legacy'), \
                    patch.object(ci, 'verify_configs'), patch.object(ci, 'tree', side_effect=[before, after]), \
                    patch.object(ci, 'ensure_release', side_effect=[Path('/new'), Path('/old')]), \
                    patch.object(ci, 'snapshot', return_value=(Path('/backup'), 'sha256:' + '1' * 64)), \
                    patch.object(ci, 'compose') as compose, \
                    patch.object(ci, 'ready', side_effect=[ci.Rejected('not ready'), None]), \
                    patch.object(ci, 'write_state') as state_write:
                with self.assertRaisesRegex(ci.Rejected, 'previous application restored; database untouched'):
                    ci.deploy(SHA)
                rollback = compose.call_args_list[-1]
                self.assertEqual(rollback.args[0], Path('/old'))
                self.assertEqual(rollback.kwargs['image'], 'sha256:' + '1' * 64)
                self.assertIn('--no-deps', rollback.args)
                state_write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
