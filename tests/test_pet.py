import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from main import BUNDLED, PetWindow
from pet_core import load_config


class PetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.temp = tempfile.TemporaryDirectory(dir=BUNDLED / 'build')
        cls.root = Path(cls.temp.name)
        cls.settings = QSettings(str(cls.root / 'position.ini'), QSettings.Format.IniFormat)
        cls.pet = PetWindow(external=cls.root, settings=cls.settings, tray=False)
        cls.pet.audio.set_muted(True)
        cls.pet.show()

    @classmethod
    def tearDownClass(cls):
        cls.pet.close()
        cls.pet.deleteLater()
        cls.app.processEvents()
        cls.temp.cleanup()

    def setUp(self):
        self.pet.reset_pose()

    def test_assets_and_window_flags(self):
        self.assertEqual(len(self.pet.images), 14)
        self.assertEqual(self.pet.current, 10)
        self.assertTrue(self.pet.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(self.pet.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(self.pet.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertTrue(self.pet.windowFlags() & Qt.WindowType.Tool)
        for number, image in self.pet.images.items():
            self.assertTrue(image.hasAlphaChannel())
            self.assertFalse(self.pet.regions[number].isEmpty())
        self.assertFalse(self.pet.mask().contains(QPoint(0, 0)))

    def test_audio_hooks_only_on_click_or_real_fx_pose_change(self):
        from fx_logic import MoodUpdate
        with patch.object(self.pet.audio, 'play') as play:
            self.pet.react()
            play.assert_called_once_with('click')
            play.reset_mock()
            for pose, mood, expected in [(1, 'up_strong', 'good'), (1, 'up_strong', None),
                    (None, 'up_strong', None), (5, 'up_strong', 'good'),
                    (2, 'up', 'good'), (6, 'flat', None), (4, 'sleep', None),
                    (3, 'down_strong', 'bad'), (7, 'down', 'bad'),
                    (13, 'panic', 'bad'), (None, 'panic', None)]:
                play.reset_mock()
                self.pet.react_to_price(MoodUpdate(pose=pose, mood=mood))
                if expected:
                    play.assert_called_once_with(expected)
                else:
                    play.assert_not_called()

    def test_mute_menu_persists_and_stops_audio(self):
        with patch.object(self.pet.audio.player, 'close') as close:
            self.pet.mute_action.setChecked(False)
            self.pet.mute_action.trigger()
            self.assertTrue(self.pet.audio.muted)
            self.assertTrue(self.settings.value('sound/muted', type=bool))
            close.assert_called_once()
            self.pet.mute_action.trigger()
            self.assertFalse(self.pet.audio.muted)
        self.pet.audio.set_muted(True)
        self.pet.mute_action.setChecked(True)

    def test_source_saved_and_okx_start_does_not_require_rectangle(self):
        from PySide6.QtCore import QTimer
        with patch.object(QTimer, 'singleShot'), patch('main.RegionSelector') as selector:
            self.pet.select_source('okx')
            saved = json.loads((self.root/'kurumi.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['data_source'], 'okx')
            self.pet.toggle_fx(True)
            self.assertTrue(self.pet.fx.active)
            self.assertEqual(self.pet.fx.source, 'okx')
            selector.assert_not_called()
            self.pet.fx.set_active(False)
            self.pet.select_source('ocr')

    def test_okx_error_fallback_and_late_response_rejection(self):
        from PySide6.QtCore import QTimer
        from okx_api import Snapshot
        monitor, notices = self.pet.fx, []
        monitor.notice.connect(notices.append)
        try:
            with patch.object(QTimer, 'singleShot'):
                monitor.set_active(True, source_override='okx')
                generation = monitor.generation
                monitor.on_okx_result(generation, Snapshot(123.45))
                self.assertEqual(monitor.last_price, 123.45)
                for _ in range(3):
                    monitor.on_okx_result(generation, None)
                self.assertEqual(monitor.source, 'ocr')
                self.assertEqual(notices, ['欸？行情连不上了……'])
                self.assertEqual(len(monitor.engine.history), 0)
                monitor.on_okx_result(generation, Snapshot(999))
                self.assertIsNone(monitor.last_price)
                self.assertEqual(self.pet.config.data_source, 'ocr')
                monitor.set_active(False)
        finally:
            monitor.notice.disconnect(notices.append)

    def test_active_menu_source_switch_rewarms_without_false_expression_or_sound(self):
        from PySide6.QtCore import QRect, QTimer
        from fx_region import region_data
        from okx_api import Snapshot
        monitor = self.pet.fx
        monitor.set_region(region_data(self.app.primaryScreen(), QRect(10, 10, 100, 30)))
        with patch.object(QTimer, 'singleShot'), patch('main.RegionSelector') as selector:
            monitor.set_active(True, source_override='okx')
            with patch.object(self.pet.audio, 'play') as play, \
                    patch('fx_monitor.time.monotonic', return_value=130.):
                for source, price in [('ocr', 150.), ('okx', 82800.)]:
                    engine = monitor.engine
                    for t in range(0, 121, 5):
                        engine.receive(82800. if source == 'ocr' else 150., float(t))
                    self.pet.select_source(source)
                    if source == 'ocr':
                        self.assertFalse(monitor.active)
                        selector.call_args.args[0](monitor.region)
                    self.assertEqual(monitor.source, source)
                    self.assertIsNot(monitor.engine, engine)
                    self.assertEqual(list(monitor.engine.history), [])
                    if source == 'ocr':
                        monitor.on_result(monitor.generation, (price, 130., False))
                    else:
                        monitor.on_okx_result(monitor.generation, Snapshot(price))
                    self.assertIsNone(monitor.engine.percent)
                    self.assertEqual(monitor.engine.mood, 'waiting')
                    self.assertEqual(self.pet.current, self.pet.config.default)
                play.assert_not_called()
            monitor.set_active(False)
            self.pet.select_source('ocr')

    def test_okx_position_and_observed_day_loss_override_price(self):
        from PySide6.QtCore import QTimer
        from okx_api import Snapshot
        monitor = self.pet.fx
        with patch.object(QTimer, 'singleShot'):
            monitor.set_active(True, source_override='okx')
            generation = monitor.generation
            self.settings.remove('fx/okx_equity/test-scope')
            monitor.on_okx_result(generation, Snapshot(100., 1000., -40., -4., 'test-scope', True))
            self.assertEqual(self.pet.text, '该止损了……真的要止损吗？')
            self.assertIn(self.pet.current, [13, 8, 14])
            self.assertIsNone(monitor.engine.stop_loss)
            # Advance beyond the hold without waiting wall time.
            monitor.engine.last_pose_at -= 30
            monitor.on_okx_result(generation, Snapshot(101., 960., -50., -5., 'test-scope', True))
            self.assertIn(self.pet.current, [8, 14])
            self.assertIn('停手', self.pet.text)
            self.assertIn('今日观测权益', monitor.status_text)
            monitor.set_active(False)

    def test_auto_recovery_from_150_ocr_to_82800_api_has_no_good_sound(self):
        from PySide6.QtCore import QTimer
        from okx_api import Snapshot
        monitor = self.pet.fx
        with patch.object(QTimer, 'singleShot'), \
                patch.object(self.pet.audio, 'play') as play, \
                patch('fx_monitor.time.monotonic', return_value=130.):
            monitor.set_active(True, source_override='okx')
            for t in range(0, 121, 5):
                monitor.engine.receive(82800., float(t))
            for _ in range(3):
                monitor.on_okx_result(monitor.generation, None)
            for t in range(0, 121, 5):
                monitor.engine.receive(150., float(t))
            monitor.okx_probe = True
            monitor.on_okx_result(monitor.generation, Snapshot(82800.))
            self.assertEqual(monitor.source, 'okx')
            self.assertEqual(list(monitor.engine.history), [(130., 82800.)])
            self.assertIsNone(monitor.engine.percent)
            self.assertEqual(monitor.engine.mood, 'waiting')
            self.assertEqual(self.pet.current, self.pet.config.default)
            play.assert_not_called()
            monitor.set_active(False)

    def test_no_repeat_and_matching_dialogue(self):
        for _ in range(150):
            before = self.pet.current
            self.pet.react()
            self.assertNotEqual(before, self.pet.current)
            self.assertIn(self.pet.text, self.pet.config.lines[self.pet.current])
            self.assertTrue(self.pet.timer.isActive())
        self.pet.reset_pose()
        self.assertEqual(self.pet.current, 10)
        self.assertEqual(self.pet.text, '')

    def test_real_timer_and_repeated_click(self):
        original = self.pet.config.duration
        try:
            self.pet.config.duration = 180
            self.pet.react()
            QTest.qWait(110)
            self.pet.react()
            QTest.qWait(110)
            self.assertTrue(self.pet.text)
            QTest.qWait(140)
            self.assertEqual(self.pet.text, '')
            self.assertEqual(self.pet.current, 10)
        finally:
            self.pet.config.duration = original

    def test_click_versus_drag_and_position(self):
        point = QPoint(160, 240)
        with patch.object(self.pet, 'react', wraps=self.pet.react) as react:
            QTest.mouseClick(self.pet, Qt.MouseButton.LeftButton, pos=point)
            self.assertEqual(react.call_count, 1)
            before = self.pet.pos()
            press_global = self.pet.mapToGlobal(point)
            QTest.mousePress(self.pet, Qt.MouseButton.LeftButton, pos=point)
            event = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(point - QPoint(40, 30)),
                                QPointF(press_global - QPoint(40, 30)),
                                Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(self.pet, event)
            QTest.mouseRelease(self.pet, Qt.MouseButton.LeftButton, pos=point)
            self.assertEqual(react.call_count, 1)
            self.assertNotEqual(before, self.pet.pos())
            self.assertEqual(self.settings.value('position'), self.pet.pos())
            saved = self.pet.pos()
            self.pet.move(1, 1)
            self.pet.restore_position()
            self.assertEqual(self.pet.pos(), saved)

    def test_config_override_bom_and_fallback(self):
        original = json.loads((BUNDLED / 'kurumi.json').read_text(encoding='utf-8'))
        external = self.root / 'override'
        external.mkdir(exist_ok=True)
        original['default'] = 3
        original['duration'] = 1200
        (external / 'kurumi.json').write_text(json.dumps(original), encoding='utf-8-sig')
        config = load_config(external, BUNDLED)
        self.assertEqual((config.default, config.duration), (3, 1200))
        (external / 'kurumi.json').write_text('{broken', encoding='utf-8')
        self.assertEqual(load_config(external, BUNDLED).default, 10)
        (external / 'kurumi.json').write_text('[]', encoding='utf-8')
        self.assertEqual(load_config(external, BUNDLED).default, 10)
        (external / 'kurumi.json').unlink()
        (external / 'assets').mkdir(exist_ok=True)
        (external / 'assets' / 'kurumi.json').write_text(json.dumps(original), encoding='utf-8')
        self.assertEqual(load_config(external, BUNDLED).default, 3)

    def test_missing_and_corrupt_image_fallback(self):
        external = self.root / 'missing-images'
        (external / 'assets').mkdir(parents=True, exist_ok=True)
        (external / 'assets' / 'kurumi_01.png').write_bytes(b'bad image')
        override = QPixmap(10, 20)
        override.fill(Qt.GlobalColor.red)
        override.save(str(external / 'assets' / 'kurumi_02.png'))
        other = PetWindow(external=external, settings=self.settings, tray=False)
        self.assertEqual(len(other.images), 14)
        self.assertEqual(other.images[1].toImage(), self.pet.images[1].toImage())
        self.assertEqual(other.images[2].toImage().pixelColor(0, 0), Qt.GlobalColor.red)
        other.close()
        other.deleteLater()

    def test_offscreen_position_is_recovered(self):
        self.pet.move(-99999, -99999)
        self.pet.keep_on_screen()
        self.assertTrue(any(s.availableGeometry().contains(self.pet.geometry())
                            for s in self.app.screens()))

    def test_transition_and_quick_repeated_clicks(self):
        self.pet.react()
        first = self.pet.current
        self.assertIsNotNone(self.pet.previous_frame)
        QTest.qWait(110)
        self.assertGreater(self.pet.transition, 0.)
        self.assertLess(self.pet.transition, 1.)
        self.pet.react()
        self.assertNotEqual(first, self.pet.current)
        QTest.qWait(330)
        self.assertIsNone(self.pet.previous_frame)
        self.assertEqual(self.pet.transition, 1.)
        self.assertGreater(self.pet.bubble_level, .99)
        self.pet.reset_pose()
        QTest.qWait(240)
        self.assertEqual(self.pet.bubble_text, '')

    def test_original_resolution_and_size_options(self):
        self.assertEqual(self.pet.images[10].width(), 1024)
        for size in (260, 420, 340):
            self.pet.set_pet_size(size)
            self.assertEqual(self.pet.width(), size+20)
            self.assertGreaterEqual(self.pet.render_images[10].width(), size)

    def test_fx_opt_in_timing_and_late_result_discard(self):
        from PySide6.QtCore import QTimer
        monitor = self.pet.fx
        self.assertFalse(monitor.active)
        self.assertEqual(monitor.timer.interval(), 3000)
        with patch.object(QTimer, 'singleShot'):
            monitor.set_active(True)
            generation = monitor.generation
            monitor.engine.stop_loss = 99
            monitor.on_result(generation, (98, monitor.engine.started, False))
            self.assertIn(self.pet.current, [13, 8, 14])
            self.assertEqual(self.pet.text, '该止损了……真的要止损吗？')
            current = self.pet.current
            self.pet.reset_pose()  # Timer only hides bubble during FX.
            self.assertEqual(self.pet.current, current)
            self.assertEqual(self.pet.text, '')
            monitor.on_result(generation, (None, monitor.engine.started+10, False))
            self.assertEqual(len(monitor.engine.history), 1)
            self.assertEqual(self.pet.current, current)
            self.pet.react()
            self.assertIn(self.pet.current, [13, 8, 14])
            self.assertNotEqual(self.pet.current, current)
            monitor.set_active(False)
            self.pet.reset_pose()
            monitor.on_result(generation, (90, 999999, False))
            self.assertEqual(self.pet.current, self.pet.config.default)
            self.assertFalse(monitor.timer.isActive())

    def test_fx_capture_uses_only_selected_rectangle_and_discards_stale(self):
        from PySide6.QtCore import QRect, QTimer
        from PySide6.QtGui import QImage
        from fx_region import region_data, resolve_region
        fixture = QImage(220, 64, QImage.Format.Format_RGB888)
        fixture.fill(Qt.GlobalColor.white)

        class SyntheticScreen:
            calls = []
            def grabWindow(screen, *args):
                screen.calls.append(args)
                return QPixmap.fromImage(fixture)
            def name(screen):
                return 'Synthetic screen'
            def geometry(screen):
                return QRect(10000, -500, 1000, 600)
            def devicePixelRatio(screen):
                return 1.5

        screen = SyntheticScreen()
        monitor = self.pet.fx
        monitor.region = region_data(screen, QRect(50, 60, 220, 64))
        with patch.object(QApplication, 'screens', return_value=[screen]), \
                patch.object(QTimer, 'singleShot'), \
                patch('fx_monitor.read_price', return_value=123.45):
            self.assertIsNotNone(resolve_region(monitor.region))
            monitor.set_active(True)
            try:
                monitor.capture()
                for _ in range(100):
                    time.sleep(.02)
                    self.app.processEvents()
                    if not monitor.busy:
                        break
                self.assertFalse(monitor.busy)
                self.assertEqual(screen.calls, [(0, 50, 60, 220, 64)])
                self.assertEqual(monitor.last_price, 123.45)
                self.assertEqual(len(monitor.engine.history), 1)
                self.assertIn('0.00%', monitor.summary())
                monitor.region['dpr'] = 2
                self.assertIsNone(resolve_region(monitor.region))
            finally:
                monitor.set_active(False)

    def test_every_ocr_enable_selects_again_even_with_saved_region(self):
        from PySide6.QtCore import QRect, QTimer
        from fx_region import region_data
        monitor = self.pet.fx
        old_region = monitor.region
        monitor.region = None
        with patch('main.RegionSelector') as selector, patch.object(QTimer, 'singleShot'):
            self.pet.toggle_fx(True)
            self.assertTrue(selector.called)
            self.assertFalse(monitor.active)
            callback = selector.call_args.args[0]
            data = region_data(self.app.primaryScreen(), QRect(10, 10, 100, 30))
            callback(data)
            self.assertTrue(monitor.active)
            self.assertEqual(json.loads(self.settings.value('fx/region')), data)
            self.pet.toggle_fx(False)
            self.assertIn('没有读到', self.pet.text)
            self.pet.toggle_fx(True)
            self.assertEqual(selector.call_count, 2)
            self.assertFalse(monitor.active)
            selector.call_args.args[0](data)
            self.assertTrue(monitor.active)
            self.assertEqual(list(monitor.engine.history), [])
            monitor.set_active(False)
        monitor.region = old_region

    def test_cancel_ocr_start_never_reuses_saved_region(self):
        from PySide6.QtCore import QRect, QTimer
        from fx_region import region_data
        monitor = self.pet.fx
        old_region = monitor.region
        data = region_data(self.app.primaryScreen(), QRect(10, 10, 100, 30))
        monitor.set_region(data)
        with patch('main.RegionSelector') as selector, patch.object(QTimer, 'singleShot'):
            self.pet.fx_action.setChecked(True)
            self.pet.toggle_fx(True)
            self.assertFalse(monitor.active)
            selector.call_args.args[0](None)
            self.assertFalse(monitor.active)
            self.assertFalse(self.pet.fx_action.isChecked())
            self.assertIsNone(self.pet.selector)
            self.assertEqual(monitor.region, data)
        monitor.region = old_region

    def test_region_selection_drag_and_escape(self):
        from fx_region import RegionOverlay
        overlay = RegionOverlay(self.app.primaryScreen())
        selected, cancelled = [], []
        overlay.selected.connect(selected.append)
        overlay.cancelled.connect(lambda: cancelled.append(True))
        overlay.show()
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(250, 180))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
        self.assertEqual(selected[0]['rect'], [100, 100, 151, 81])
        QTest.keyClick(overlay, Qt.Key.Key_Escape)
        self.assertEqual(cancelled, [True])
        overlay.close()
        overlay.deleteLater()

    def test_settings_stop_loss_and_privacy(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QLineEdit
        observations = []
        def fill():
            dialog = QApplication.activeModalWidget()
            observations.append(isinstance(dialog, QDialog))
            observations.append(any('不保存、不上传' in label.text() for label in dialog.findChildren(QLabel)))
            dialog.findChild(QLineEdit, 'fx_stop_loss').setText('98.50')
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
        QTimer.singleShot(50, fill)
        self.pet.fx_settings()
        self.assertEqual(observations, [True, True])
        self.assertEqual(self.pet.fx.stop_loss(), 98.5)
        self.settings.setValue('fx/stop_loss', '')

    def test_settings_opens_clear_of_pet_without_moving_pet(self):
        from PySide6.QtCore import QTimer
        original = self.pet.pos()
        observations = []
        def inspect():
            dialog = QApplication.activeModalWidget()
            observations.append(dialog is not None and not dialog.frameGeometry().intersects(self.pet.frameGeometry()))
            observations.append(self.pet.pos() == original)
            observations.append(any(screen.availableGeometry().contains(dialog.frameGeometry())
                                    for screen in QApplication.screens()))
            dialog.reject()
        QTimer.singleShot(50, inspect)
        self.pet.fx_settings()
        self.assertEqual(observations, [True, True, True])

    def test_permission_warning_opens_clear_of_pet(self):
        original = self.pet.pos()
        self.pet.show_permission_warning('建议改成只读。公共行情仍可使用。')
        self.app.processEvents()
        try:
            self.assertFalse(self.pet.permission_box.frameGeometry().intersects(self.pet.frameGeometry()))
            self.assertEqual(self.pet.pos(), original)
        finally:
            self.pet.permission_box.reject()


if __name__ == '__main__':
    (BUNDLED / 'build').mkdir(exist_ok=True)
    unittest.main(verbosity=2)
