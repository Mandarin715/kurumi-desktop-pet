"""Opt-in capture and diagnostic regressions; synthetic prices and silent audio only."""
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QRect, QSettings, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication
from fx_debug import LOGGER
from fx_logic import MoodUpdate, PriceMoodEngine, validated_policy
from main import PetWindow
from okx_api import Snapshot
from pet_audio import AsyncMP3Player, PetAudio
from pet_core import load_config


class DebugTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'build')
        self.settings = QSettings(str(Path(self.temp.name)/'test.ini'), QSettings.Format.IniFormat)
        self.pet = PetWindow(external=Path(self.temp.name), bundled=ROOT, settings=self.settings, tray=False)
        self.pet.audio.set_muted(True)
        self.monitor = self.pet.fx
        self.monitor.policy = validated_policy({'debug_log': True})
        self.monitor.engine = PriceMoodEngine(self.monitor.policy)
        self.monitor.history_scope = self.monitor.price_scope()
        self.monitor.active = True
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.old_propagate = LOGGER.propagate
        LOGGER.propagate = False
        LOGGER.addHandler(self.handler)

    def tearDown(self):
        self.pet.close()
        self.pet.deleteLater()
        self.app.processEvents()
        LOGGER.removeHandler(self.handler)
        LOGGER.propagate = self.old_propagate
        self.temp.cleanup()

    def records(self, event=None):
        rows = [json.loads(line.split('FX_DEBUG ', 1)[1]) for line in self.stream.getvalue().splitlines()]
        return [row for row in rows if event is None or row['event'] == event]

    def test_defaults_and_boolean_validation(self):
        defaults = validated_policy()
        self.assertIs(defaults['exclude_from_capture'], True)
        self.assertIs(defaults['debug_log'], False)
        custom = validated_policy({'exclude_from_capture': False, 'debug_log': True, 'hold_seconds': 31})
        self.assertFalse(custom['exclude_from_capture'])
        self.assertTrue(custom['debug_log'])
        self.assertEqual(custom['hold_seconds'], 31)
        for key in ('exclude_from_capture', 'debug_log'):
            for value in (0, 1, 'false', None):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    validated_policy({key: value})

    def test_capture_flag_sets_none_or_exclude_on_enable_and_clears_on_stop(self):
        with patch.object(QTimer, 'singleShot'), patch.object(self.monitor, 'capture_affinity', return_value=True) as affinity:
            for exclude, flag in ((True, 0x11), (False, 0)):
                self.monitor.policy['exclude_from_capture'] = exclude
                self.monitor.set_active(True, source_override='ocr')
                affinity.assert_called_with(flag)
                self.assertEqual(self.monitor.apply_capture_policy(), exclude)
                self.monitor.set_active(False)
                affinity.assert_called_with(0)

    def test_external_json_switches_load_and_leave_other_values_intact(self):
        folder = Path(self.temp.name)
        data = json.loads((ROOT/'kurumi.json').read_text(encoding='utf-8-sig'))
        data.setdefault('fx', {}).update(exclude_from_capture=False, debug_log=True, hold_seconds=31)
        path = folder/'kurumi.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        original = path.read_bytes()
        config = load_config(folder, ROOT)
        self.assertIs(config.fx['exclude_from_capture'], False)
        self.assertIs(config.fx['debug_log'], True)
        self.assertEqual(config.fx['hold_seconds'], 31)
        self.assertEqual(path.read_bytes(), original)

    def test_disabled_debug_produces_no_sample_pose_or_audio_records(self):
        self.monitor.policy['debug_log'] = False
        self.monitor.busy = True
        self.monitor.capture()
        self.monitor.on_result(self.monitor.generation, (None, 0., False))
        self.assertIsNone(self.monitor.pose_audio_reporter(10, 1, MoodUpdate(pose=1)))
        self.assertEqual(self.records(), [])

    def test_every_guarded_tick_logs_its_distinct_skip_reason(self):
        m = self.monitor
        m.busy = True
        m.capture()
        m.busy = False
        self.pet.dragging = True
        m.capture()
        self.pet.dragging = False
        with patch.object(self.pet.menu, 'isVisible', return_value=True): m.capture()
        with patch.object(QApplication, 'activeModalWidget', return_value=object()): m.capture()
        with patch('fx_monitor.resolve_region', return_value=None): m.capture()
        rows = self.records('sample')
        self.assertEqual([r['reason'] for r in rows], ['busy', 'dragging', 'menu_open', 'settings_open', 'invalid_region'])
        self.assertEqual([r['sample_id'] for r in rows], list(range(1, 6)))
        self.assertTrue(all(r['skipped'] and r['price'] is None and r['percent'] is None for r in rows))
        self.assertTrue(all('sample_time' in r and 'monotonic_s' in r and r['elapsed_ms'] >= 0 for r in rows))

    def test_capture_visible_pet_still_skips_occluded_price_without_exclusion(self):
        self.monitor.policy['exclude_from_capture'] = False
        rect = self.pet.geometry()
        screen = Mock()
        screen.geometry.return_value = QRect(0, 0, 3000, 2000)
        with patch('fx_monitor.resolve_region', return_value=(screen, rect)), \
                patch.object(self.monitor, 'capture_affinity', return_value=True) as affinity:
            self.monitor.capture()
            affinity.assert_called_once_with(0)
            screen.grabWindow.assert_not_called()
        self.assertEqual(self.records('sample')[-1]['reason'], 'pet_occludes_region')

    def test_capture_errors_and_empty_image_are_logged(self):
        screen = Mock()
        screen.geometry.return_value = QRect(10000, 10000, 1000, 1000)
        with patch('fx_monitor.resolve_region', return_value=(screen, QRect(0, 0, 200, 60))), \
                patch.object(self.monitor, 'capture_affinity', return_value=True):
            screen.grabWindow.side_effect = RuntimeError()
            self.monitor.capture()
            screen.grabWindow.side_effect = None
            screen.grabWindow.return_value = QPixmap()
            self.monitor.capture()
        self.assertEqual([r['reason'] for r in self.records('sample')], ['capture_error', 'capture_empty'])

    def test_ocr_success_window_and_failed_stale_results(self):
        m = self.monitor
        for t in range(0, 120, 10): m.engine.receive(100., t)
        m.engine.receive(102., 119.)
        m.pending_sample = m.sample_context()
        m.on_result(m.generation, (102., 120., False))
        row = self.records('sample')[-1]
        self.assertEqual((row['price'], row['percent'], row['mood'], row['skipped']), (102., 2., 'up_strong', False))
        self.assertEqual((row['long_percent'], row['fast_window_seconds'], row['window_seconds']), (2., 30, 120))
        self.assertFalse(row['warming'])
        self.assertEqual(row['span_seconds'], 30.)
        for price, error in ((None, False), (None, True)):
            m.on_result(m.generation, (price, 130., error))
        m.on_result(m.generation-1, (999., 130., False))
        self.assertEqual([r['reason'] for r in self.records('sample')],
                         ['accepted', 'ocr_unreadable', 'ocr_error', 'stale_result'])
        self.assertEqual(m.last_price, 102.)

    def test_okx_acceptance_and_fallback_have_sample_records(self):
        m = self.monitor
        m.source = 'okx'
        m.on_okx_result(m.generation, Snapshot(123.45))
        with patch.object(QTimer, 'singleShot'):
            for _ in range(3):
                m.on_okx_result(m.generation, None)
        rows = self.records('sample')
        self.assertEqual([r['reason'] for r in rows],
                         ['accepted', 'okx_error_retry', 'okx_error_retry', 'okx_error_fallback'])
        self.assertEqual([r.get('consecutive_failures') for r in rows[1:]], [1, 2, 3])
        self.assertEqual(rows[0]['source'], 'okx')
        self.assertEqual(rows[0]['price'], 123.45)
        self.assertEqual(m.source, 'ocr')
        self.assertFalse(any(name in self.stream.getvalue() for name in ('api_key', 'secret_key', 'passphrase', 'OK-ACCESS')))

    def test_pose_log_contains_exact_sound_and_actual_result_not_just_queue(self):
        played = threading.Event()
        class Player:
            alias = 'silent-test'
            def open(self, path): return True
            def command(self, command): return True, ''
            def close(self): pass
        audio = self.pet.audio
        audio.set_muted(False)
        audio.player = AsyncMP3Player(Player)
        report = self.monitor.pose_audio_reporter(10, 1, MoodUpdate(pose=1, mood='up_strong', percent=2.))
        def callback(result):
            report(result)
            if result['audio_status'] == 'played': played.set()
        with patch('pet_audio.random.choice', return_value='good_2'):
            self.assertTrue(audio.fx_pose_changed(10, 1, on_result=callback))
        self.assertTrue(played.wait(2))
        rows = self.records()
        self.assertEqual([r['event'] for r in rows], ['pose', 'audio_result'])
        self.assertIsNone(rows[0]['audio_played'])
        self.assertIs(rows[1]['audio_played'], True)
        self.assertEqual(rows[0]['sound'], 'good_2')
        self.assertEqual(rows[1]['pose_id'], rows[0]['pose_id'])
        self.assertEqual(rows[1]['pose'], 1)
        audio.close()
        audio.player.thread.join(2)

    def test_pose_logging_muted_flat_and_unchanged(self):
        m = self.monitor
        for pose, mood in ((1, 'up_strong'), (6, 'flat')):
            self.pet.react_to_price(MoodUpdate(pose=pose, mood=mood))
        self.pet.react_to_price(MoodUpdate(pose=6, mood='flat'))
        rows = self.records('pose')
        self.assertEqual([r['pose'] for r in rows], [1, 6])
        self.assertEqual([r['audio_status'] for r in rows], ['muted', 'no_effect'])
        self.assertTrue(all(r['audio_played'] is False for r in rows))


if __name__ == '__main__': unittest.main(verbosity=2)
