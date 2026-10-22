"""Offline updater tests: fixed public endpoints, integrity and transactional rollback."""
from copy import deepcopy
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile, ZipInfo

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_version import VERSION
from updater import (BAIDU_URL, QUARK_URL, LATEST_URL, REPOSITORY, Cancelled, GitHubRedirects,
                     Release, UpdateError, check_latest, digest_file, download_release,
                     install_transaction, parse_release, prepare_install, slow_download,
                     stage_archive, version_tuple, write_start_receipt)


def metadata(version='9.0.0', digest='a'*64):
    name = f'Kurumi-v{version}-Windows-x64.zip'
    return {'tag_name': 'v'+version, 'draft': False, 'prerelease': False,
            'body': '修复 XPERP；增加自动更新。', 'assets': [{
                'name': name, 'browser_download_url': f'https://github.com/{REPOSITORY}/releases/download/v{version}/{name}',
                'size': 100, 'digest': 'sha256:'+digest}]}


class ReleaseTests(unittest.TestCase):
    def test_numeric_versions_and_stable_release_filter(self):
        self.assertGreater(version_tuple('v1.10.0'), version_tuple('1.9.9'))
        for invalid in ('1.2', 'v1.2.3-beta', '../1.2.3', None):
            with self.assertRaises(ValueError):
                version_tuple(invalid)
        self.assertIsNotNone(parse_release(metadata(), current='1.2.0'))
        for changes in ({'draft': True}, {'prerelease': True}, {'tag_name': 'v9.0.0-rc1'}):
            self.assertIsNone(parse_release({**metadata(), **changes}))
        self.assertIsNone(parse_release(metadata('1.1.1')))
        self.assertIsNone(parse_release(metadata(VERSION)))

    def test_skip_applies_only_to_exact_version(self):
        self.assertIsNone(parse_release(metadata('2.0.0'), skipped='2.0.0'))
        self.assertIsNotNone(parse_release(metadata('2.0.1'), skipped='2.0.0'))

    def test_requires_valid_asset_size_checksum_and_repository_url(self):
        for field, value in [('size', 0), ('size', True), ('size', 1024**3),
                             ('digest', 'sha256:wrong'), ('browser_download_url', 'https://example.com/file.zip'),
                             ('name', 'Kurumi.exe')]:
            data = metadata()
            data['assets'][0][field] = value
            self.assertIsNone(parse_release(data), field)
        data = metadata()
        data['assets'] *= 2
        self.assertIsNone(parse_release(data))

    def test_fallback_checksum_manifest_and_exact_filename(self):
        data = metadata()
        data['assets'][0].pop('digest')
        data['assets'].append({'name': 'SHA256SUMS.txt', 'browser_download_url':
            f'https://github.com/{REPOSITORY}/releases/download/v9.0.0/SHA256SUMS.txt'})
        name = data['assets'][0]['name']
        result = parse_release(data, checksum_loader=lambda _: (f'{"b"*64}  {name}\n').encode())
        self.assertEqual(result.sha256, 'b'*64)
        self.assertIsNone(parse_release(data, checksum_loader=lambda _: (f'{"b"*64}  other.zip\n').encode()))

    def test_network_json_http_failures_are_silent(self):
        for failure in (TimeoutError(), OSError(), ValueError()):
            self.assertIsNone(check_latest(opener=Mock(side_effect=failure)))
        self.assertIsNone(check_latest(opener=lambda _: BytesIO(b'not json')))
        requests = []
        def opener(url):
            requests.append(url)
            return BytesIO(json.dumps(metadata()).encode())
        self.assertIsNotNone(check_latest(opener=opener))
        self.assertEqual(requests, [LATEST_URL])

    def test_redirects_reject_insecure_or_unrelated_hosts(self):
        from urllib.request import Request
        handler = GitHubRedirects()
        request = Request('https://github.com/a')
        for url in ('http://github.com/a', 'file:///a', 'https://evil.example/a',
                    'https://github.com.evil.example/a', 'https://user:pass@github.com/a',
                    'https://github.com:444/a'):
            with self.assertRaises(UpdateError):
                handler.redirect_request(request, None, 302, '', {}, url)
        redirect = handler.redirect_request(request, None, 302, '', {},
                                            'https://release-assets.githubusercontent.com/github-production/a')
        self.assertEqual(redirect.host, 'release-assets.githubusercontent.com')


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'build')
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def release(self, content):
        return Release('9.0.0', 'v9.0.0', 'notes', 'test.zip', 'https://github.com/fixture',
                       len(content), hashlib.sha256(content).hexdigest())

    def test_download_progress_checksum_and_exact_length(self):
        content = b'MZ'+b'x'*150000
        release = self.release(content)
        progress = []
        destination = self.folder/'download.zip'
        download_release(release, destination, threading.Event(), lambda *args: progress.append(args),
                         opener=lambda *args, **kwargs: BytesIO(content))
        self.assertEqual(destination.read_bytes(), content)
        self.assertEqual(progress[-1], (len(content), len(content)))
        self.assertGreater(len(progress), 1)

    def test_wrong_size_hash_network_and_cancel_leave_no_partial(self):
        destination = self.folder/'download.zip'
        for content in (b'abc', b'wrong', b'abcdextra'):
            with self.assertRaises(UpdateError):
                download_release(self.release(b'abcd'), destination, threading.Event(),
                                 opener=lambda *args, **kwargs: BytesIO(content))
            self.assertFalse(destination.exists())
        with self.assertRaises(UpdateError):
            download_release(self.release(b'abcd'), destination, threading.Event(),
                             opener=Mock(side_effect=TimeoutError('private details')))
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            download_release(self.release(b'abcd'), destination, cancel)
        self.assertFalse(destination.exists())

    def test_cancel_during_download_removes_partial_without_install(self):
        cancel = threading.Event()
        destination = self.folder/'download.zip'
        content = b'x'*150000
        with self.assertRaises(Cancelled):
            download_release(self.release(content), destination, cancel,
                             lambda *_: cancel.set(), opener=lambda *a, **k: BytesIO(content))
        self.assertFalse(destination.exists())

    def archive(self, extra=None, manifest_change=None):
        content = {'Kurumi.exe': b'MZnew-exe', '使用说明.txt': b'new docs'}
        manifest = {'version': '9.0.0', 'platform': 'windows-x64',
                    'files': {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}}
        if manifest_change:
            manifest_change(manifest)
        path = self.folder/'package.zip'
        with ZipFile(path, 'w') as output:
            for name, data in content.items():
                output.writestr('Kurumi/'+name, data)
            output.writestr('Kurumi/update-manifest.json', json.dumps(manifest))
            output.writestr('Kurumi/kurumi.json', 'never overwrite me')
            output.writestr('Kurumi/assets/kurumi_01.png', 'never overwrite custom images')
            for name, data in (extra or {}).items():
                output.writestr(name, data)
        return path

    def test_stage_exact_allowlist_ignores_config_and_custom_assets(self):
        stage = self.folder/'stage'
        files = stage_archive(self.archive(), stage, '9.0.0')
        self.assertEqual(set(files), {'Kurumi.exe', '使用说明.txt'})
        self.assertEqual({path.name for path in stage.iterdir()}, set(files))
        self.assertEqual((stage/'Kurumi.exe').read_bytes(), b'MZnew-exe')

    def test_stage_rejects_traversal_symlinks_duplicates_hash_and_manifest_version(self):
        for name in ('Kurumi/../../bad', '/Kurumi/bad', 'Kurumi/..\\bad', 'Kurumi/C:bad'):
            with self.assertRaises(UpdateError):
                stage_archive(self.archive({name: 'evil'}), self.folder/'stage', '9.0.0')
            self.assertFalse((self.folder/'stage').exists())
        for change in (lambda m: m.update(version='8.0.0'),
                       lambda m: m['files'].update({'okx.local.json': 'a'*64}),
                       lambda m: m['files'].update({'Kurumi.exe': 'a'*64})):
            with self.assertRaises(UpdateError):
                stage_archive(self.archive(manifest_change=change), self.folder/'stage', '9.0.0')
        path = self.archive()
        with ZipFile(path, 'a') as output:
            link = ZipInfo('Kurumi/link')
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(link, 'elsewhere')
        with self.assertRaises(UpdateError):
            stage_archive(path, self.folder/'stage', '9.0.0')

    def setup_transaction(self):
        work, target = self.folder/'work', self.folder/'install'/'Kurumi.exe'
        work.mkdir()
        target.parent.mkdir()
        target.write_bytes(b'MZold-exe')
        (target.parent/'使用说明.txt').write_bytes(b'old docs')
        (target.parent/'kurumi.json').write_bytes(b'personal config exact bytes')
        (target.parent/'okx.local.json').write_bytes(b'personal keys exact bytes')
        (target.parent/'assets').mkdir()
        (target.parent/'assets'/'custom.png').write_bytes(b'custom image')
        files = stage_archive(self.archive(), work/'stage', '9.0.0')
        return work, target, files

    def assert_personal_files(self, target):
        self.assertEqual((target.parent/'kurumi.json').read_bytes(), b'personal config exact bytes')
        self.assertEqual((target.parent/'okx.local.json').read_bytes(), b'personal keys exact bytes')
        self.assertEqual((target.parent/'assets'/'custom.png').read_bytes(), b'custom image')

    def test_transaction_updates_restarts_and_preserves_personal_files(self):
        work, target, files = self.setup_transaction()
        def launch(executable, arguments=()):
            self.assertEqual(executable, target)
            Path(arguments[1]).write_text(json.dumps({'executable': str(target)}))
            return Mock(poll=lambda: None)
        self.assertTrue(install_transaction(work, target, files, launch=launch, health_timeout=.1))
        self.assertEqual(target.read_bytes(), b'MZnew-exe')
        self.assertEqual((target.parent/'使用说明.txt').read_bytes(), b'new docs')
        self.assert_personal_files(target)

    def test_failed_new_start_rolls_back_every_file_and_restarts_old(self):
        work, target, files = self.setup_transaction()
        calls = []
        def launch(executable, arguments=()):
            calls.append(list(arguments))
            return Mock(poll=lambda: 1)
        self.assertFalse(install_transaction(work, target, files, launch=launch, health_timeout=.1))
        self.assertEqual(target.read_bytes(), b'MZold-exe')
        self.assertEqual((target.parent/'使用说明.txt').read_bytes(), b'old docs')
        self.assertEqual(calls[-1], [])
        self.assert_personal_files(target)

    def test_locked_exe_rolls_back_already_updated_document(self):
        import updater
        work, target, files = self.setup_transaction()
        replace = updater.replace_retry
        def locked(source, destination, *args):
            if Path(source).read_bytes() == b'MZnew-exe':
                raise PermissionError()
            return replace(source, destination, *args)
        launch = Mock()
        with patch('updater.replace_retry', side_effect=locked):
            self.assertFalse(install_transaction(work, target, files, launch=launch))
        self.assertEqual(target.read_bytes(), b'MZold-exe')
        self.assertEqual((target.parent/'使用说明.txt').read_bytes(), b'old docs')
        launch.assert_called_once_with(target)
        self.assert_personal_files(target)

    def test_stage_changed_after_validation_never_replaces_target(self):
        work, target, files = self.setup_transaction()
        (work/'stage'/'Kurumi.exe').write_bytes(b'MZtampered')
        with self.assertRaises(UpdateError):
            install_transaction(work, target, files, launch=Mock())
        self.assertEqual(target.read_bytes(), b'MZold-exe')
        self.assert_personal_files(target)

    def test_prepare_helper_uses_own_exe_without_config_and_no_target_writes(self):
        work, target, files = self.setup_transaction()
        before = target.read_bytes()
        plan, helper = prepare_install(work, target, files, parent_pid=123, executable=target)
        self.assertEqual(helper.read_bytes(), before)
        saved = json.loads(plan.read_text())
        self.assertEqual(saved['parent_pid'], 123)
        self.assertNotIn('personal', plan.read_text())
        self.assertEqual(target.read_bytes(), before)
        self.assert_personal_files(target)

    def test_receipt_only_written_for_matching_helper_session(self):
        folder = self.folder/'KurumiUpdate-test'
        folder.mkdir()
        target = self.folder/'Kurumi.exe'
        target.write_bytes(b'MZ')
        plan = folder/'install.json'
        plan.write_text(json.dumps({'target': str(target)}))
        with patch('updater.sys.executable', str(target)):
            write_start_receipt(folder/'started.json')
        result = json.loads((folder/'started.json').read_text())
        self.assertEqual(result['executable'], str(target))
        self.assertEqual(result['version'], VERSION)
        (folder/'started.json').unlink()
        with patch('updater.sys.executable', str(target.with_name('other.exe'))):
            write_start_receipt(folder/'started.json')
        self.assertFalse((folder/'started.json').exists())

    def test_helper_ready_precedes_parent_wait_and_install_uses_own_session(self):
        from updater import run_installer
        work, target, files = self.setup_transaction()
        plan, helper = prepare_install(work, target, files, parent_pid=123, executable=target)
        def wait(pid):
            self.assertEqual(pid, 123)
            self.assertTrue((work/'helper-ready').is_file())
            return True
        with patch('updater.sys.executable', str(helper)), patch('updater.wait_for_process', side_effect=wait), \
                patch('updater.install_transaction', return_value=True) as install:
            self.assertEqual(run_installer(plan), 0)
            install.assert_called_once_with(work, str(target), files)
        self.assertTrue(json.loads((work/'result.json').read_text())['ok'])

    def test_helper_rejects_other_folder_and_changed_stage_before_pet_exits(self):
        from updater import run_installer
        work, target, files = self.setup_transaction()
        plan, helper = prepare_install(work, target, files, parent_pid=123, executable=target)
        with patch('updater.sys.executable', str(target)), patch('updater.wait_for_process') as wait:
            self.assertEqual(run_installer(plan), 1)
            wait.assert_not_called()
        (work/'stage'/'Kurumi.exe').write_bytes(b'MZchanged')
        with patch('updater.sys.executable', str(helper)), patch('updater.wait_for_process') as wait:
            self.assertEqual(run_installer(plan), 1)
            wait.assert_not_called()
        self.assertFalse((work/'helper-ready').exists())

    def test_public_archive_has_latest_tutorial_and_default_config_without_keys(self):
        from release_package import build_package
        from pet_audio import SOUND_NAMES
        root = self.folder/'project'
        dist = root/'dist'
        dist.mkdir(parents=True)
        for name in ('README.md', '使用说明.txt', 'LICENSES.txt'):
            (root/name).write_text('public docs', encoding='utf-8')
        (root/'kurumi.json').write_text(json.dumps({'default': 10, 'duration': 4000,
            'lines': {'1': ['hello']}, 'okx': {'inst_id': 'PERSONAL-USDT', 'api_key': 'SECRET'}}))
        (dist/'Kurumi.exe').write_bytes(b'MZfixture')
        (dist/'久留美桌宠-使用教程.docx').write_bytes(b'LATEST TUTORIAL')
        personal = {'kurumi.json': b'PRIVATE CONFIG', 'okx.local.json': b'PRIVATE KEYS'}
        for name, value in personal.items():
            (dist/name).write_bytes(value)
        (root/'assets'/'sfx').mkdir(parents=True)
        for number in range(1, 15):
            (root/'assets'/f'kurumi_{number:02d}.png').write_bytes(b'png')
        for name in SOUND_NAMES:
            (root/'assets'/'sfx'/f'{name}.mp3').write_bytes(b'mp3')
        archive, checksum = build_package(root)
        with ZipFile(archive) as package:
            self.assertNotIn('Kurumi/okx.local.json', package.namelist())
            self.assertEqual(package.read('Kurumi/久留美桌宠-使用教程.docx'), b'LATEST TUTORIAL')
            config = package.read('Kurumi/kurumi.json')
            self.assertNotIn(b'SECRET', config)
            self.assertNotIn(b'PRIVATE', config)
            self.assertEqual(json.loads(config)['okx']['inst_id'], 'BTC-USDT')
            self.assertEqual(json.loads(config)['sound']['volume'], .3)
        self.assertTrue(checksum.read_text().startswith(digest_file(archive)))
        for name, value in personal.items():
            self.assertEqual((dist/name).read_bytes(), value)
        self.assertIn('Kurumi.exe', stage_archive(archive, self.folder/'verified-package', VERSION))

    def test_cleanup_only_old_completed_own_update_sessions(self):
        from updater import cleanup_finished_sessions
        import time
        complete = self.folder/'KurumiUpdate-complete'
        active = self.folder/'KurumiUpdate-active'
        unrelated = self.folder/'personal'
        for folder in (complete, active, unrelated):
            folder.mkdir()
            (folder/'keep.exe').write_bytes(b'test')
        (complete/'result.json').write_text('{"ok":true}')
        (complete/'install.json').write_text(json.dumps({'target': 'Kurumi.exe', 'files': {'Kurumi.exe': 'a'*64}}))
        for folder in (complete, active, unrelated):
            os.utime(folder, (time.time()-600, time.time()-600))
        cleanup_finished_sessions(self.folder)
        self.assertFalse(complete.exists())
        self.assertTrue(active.exists())
        self.assertTrue(unrelated.exists())


class SlowTests(unittest.TestCase):
    def test_idle_and_slow_thresholds_do_not_interrupt_healthy_download(self):
        self.assertTrue(slow_download(20, 20, 0))
        self.assertTrue(slow_download(30, 0, 30*99*1024))
        self.assertFalse(slow_download(29, 0, 1))
        self.assertFalse(slow_download(30, 0, 30*100*1024))
        self.assertFalse(slow_download(60, 1, 60*1024*1024))


class UpdateWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QWidget
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'build')
        self.pet = QWidget()
        self.pet.resize(200, 200)
        self.pet.settings = QSettings(str(Path(self.temp.name)/'test.ini'), QSettings.Format.IniFormat)
        self.pet.selector = None

    def tearDown(self):
        self.pet.close()
        self.pet.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_dialog_notes_are_plain_text_and_mirror_urls_exact(self):
        from update_ui import UpdateDialog
        release = parse_release({**metadata(), 'body': '<img src="https://evil.example">\n修复说明'})
        dialog = UpdateDialog(release, self.pet)
        self.assertEqual(dialog.notes.toPlainText(), release.notes)
        self.assertLessEqual(dialog.width(), 500)
        with patch('update_ui.QDesktopServices.openUrl') as opened:
            dialog.baidu.click()
            self.assertEqual(opened.call_args.args[0].toString(), BAIDU_URL)
            dialog.quark.click()
            self.assertEqual(opened.call_args.args[0].toString(), QUARK_URL)
        skipped = []
        dialog.skip_requested.connect(skipped.append)
        dialog.skip.click()
        self.assertEqual(skipped, ['9.0.0'])

    def test_controller_skip_persists_outside_json_and_later_does_not_skip(self):
        from update_ui import UpdateController
        controller = UpdateController(self.pet)
        release = parse_release(metadata())
        controller.offer(release)
        controller.dialog.later.click()
        self.assertEqual(self.pet.settings.value('updates/skipped', ''), '')
        controller.offer(release)
        controller.dialog.skip.click()
        self.assertEqual(self.pet.settings.value('updates/skipped'), '9.0.0')
        self.assertFalse(list(Path(self.temp.name).glob('*.json')))

    def test_slow_or_failed_download_keeps_mirrors_and_original_window(self):
        from update_ui import UpdateController
        controller = UpdateController(self.pet)
        controller.offer(parse_release(metadata()))
        controller.dialog.set_downloading()
        controller.download_worker = Mock()
        controller.started, controller.last_data, controller.received = 0., 0., 0
        with patch('update_ui.time.monotonic', return_value=21.):
            controller.check_progress()
        self.assertIn('较慢', controller.dialog.status.text())
        self.assertTrue(controller.dialog.baidu.isEnabled())
        self.assertTrue(controller.dialog.quark.isEnabled())
        controller.on_failed('GitHub 下载失败，请使用网盘。')
        self.assertTrue(controller.dialog.update_button.isEnabled())
        self.assertIn('网盘', controller.dialog.status.text())
        controller.dialog.reject()

    def test_cancel_is_allowed_during_download_but_not_after_helper_handoff(self):
        from update_ui import UpdateDialog
        dialog = UpdateDialog(parse_release(metadata()), self.pet)
        cancelled = []
        dialog.cancel_requested.connect(lambda: cancelled.append(True))
        dialog.show()
        dialog.set_downloading()
        dialog.installing = True
        dialog.reject()
        self.assertTrue(dialog.isVisible())
        self.assertEqual(cancelled, [])
        dialog.installing = False
        dialog.reject()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(cancelled, [True])


if __name__ == '__main__':
    unittest.main()
