"""Network recovery and daily baselines: fake API, synthetic prices, isolated settings."""
from datetime import date
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QEventLoop, QObject, QSettings, QThreadPool, QTimer
from PySide6.QtWidgets import QApplication
from fx_logic import PriceMoodEngine, validated_policy
from fx_monitor import FXMonitor, OKX_FAILURE_LIMIT, OKX_RETRY_SECONDS
from main import PetWindow
from okx_api import DEFAULT_OKX, Snapshot


class FakePet(QObject):
    def __init__(self, folder):
        super().__init__()
        self.config = SimpleNamespace(fx=validated_policy(), data_source='okx', okx=DEFAULT_OKX.copy())
        self.settings = QSettings(str(folder/'test.ini'), QSettings.Format.IniFormat)
        self.current, self.external, self.dragging = 10, folder, False
        self.menu = Mock()
        self.menu.isVisible.return_value = False


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'build')
        self.pet = FakePet(Path(self.temp.name))
        self.m = FXMonitor(self.pet)
        self.shots = patch.object(QTimer, 'singleShot').start()
        patch.object(self.m, 'capture_affinity', return_value=True).start()
        self.m.set_active(True)
        self.m.timer.stop()
        self.m.rest_timer.stop()
        self.m.account_timer.stop()
        self.m.client = Mock()
        self.notices = []
        self.m.notice.connect(self.notices.append)

    def tearDown(self):
        self.m.set_active(False)
        patch.stopall()
        self.pet.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def seed_window(self, price=100.):
        self.m.engine = PriceMoodEngine(self.m.policy)
        self.m.history_scope = self.m.price_scope()
        for t in range(0, 121, 5):
            self.m.engine.receive(price, float(t))
        return self.m.engine, list(self.m.engine.history)

    def fail(self, count=1):
        for _ in range(count):
            self.m.on_okx_result(self.m.generation, None)

    def run_retry(self, now, snapshot):
        self.m.client = Mock()
        self.m.client.poll_ticker.return_value = snapshot
        if snapshot is None:
            self.m.client.poll_ticker.side_effect = TimeoutError('synthetic timeout')
        else:
            self.m.client.poll_ticker.side_effect = None
        with patch('fx_monitor.time.monotonic', return_value=now), \
                patch('fx_monitor.QThreadPool.globalInstance') as pool:
            self.m.capture()
            pool.return_value.start.assert_called_once()
            self.m.job.run()

    def test_isolated_failures_reset_counter_and_keep_price_window(self):
        engine, history = self.seed_window()
        with patch('fx_monitor.time.monotonic', return_value=125.):
            self.fail(2)
            self.assertEqual(self.m.source, 'okx')
            self.assertEqual(self.m.okx_failures, 2)
            self.assertEqual(list(engine.history), history)
            self.m.on_okx_result(self.m.generation, Snapshot(100.1))
            self.assertEqual(self.m.okx_failures, 0)
            self.fail(2)
        self.assertEqual(self.m.source, 'okx')
        self.assertIs(self.m.engine, engine)
        self.assertEqual(self.notices, [])

    def test_consecutive_failures_fallback_and_recovery_both_rewarm(self):
        engine, history = self.seed_window()
        with patch('fx_monitor.time.monotonic', return_value=150.):
            self.fail(OKX_FAILURE_LIMIT)
        self.assertEqual(self.m.source, 'ocr')
        self.assertEqual(self.notices, ['欸？行情连不上了……'])
        self.assertIsNot(self.m.engine, engine)
        self.assertEqual(list(self.m.engine.history), [])
        self.assertEqual(self.m.okx_retry_at, 150.+OKX_RETRY_SECONDS)
        # Recovery works even if there is no valid OCR rectangle. A failed probe
        # stays in OCR; its next retry is scheduled rather than permanently off.
        self.run_retry(180., None)
        self.assertEqual(self.m.source, 'ocr')
        self.assertEqual(self.m.okx_retry_at, 210.)
        self.assertEqual(self.notices, ['欸？行情连不上了……'])
        ocr_engine = self.m.engine
        self.run_retry(210., Snapshot(100.1))
        self.assertEqual(self.m.source, 'okx')
        self.assertIsNot(self.m.engine, ocr_engine)
        self.assertEqual(list(self.m.engine.history), [(210., 100.1)])
        self.assertIsNone(self.m.engine.percent)
        self.assertEqual(self.m.engine.mood, 'waiting')
        self.assertEqual(self.m.okx_failures, 0)
        self.assertIsNone(self.m.okx_retry_at)
        self.assertEqual(self.pet.config.data_source, 'okx')

    def test_retry_waits_for_deadline_and_ocr_keeps_sampling_between_probes(self):
        with patch('fx_monitor.time.monotonic', return_value=100.):
            self.fail(3)
        with patch('fx_monitor.time.monotonic', return_value=129.99), \
                patch('fx_monitor.resolve_region', return_value=None), \
                patch('fx_monitor.QThreadPool.globalInstance') as pool:
            self.m.capture()
            pool.return_value.start.assert_not_called()
        self.m.on_result(self.m.generation, (100., 130., False))
        self.assertEqual(self.m.last_price, 100.)
        self.run_retry(130., Snapshot(100.))
        self.assertEqual(self.m.source, 'okx')

    def test_user_selecting_ocr_disables_api_retries_and_clears_history(self):
        engine, history = self.seed_window()
        self.pet.config.data_source = 'ocr'
        self.m.set_active(True, preserve_history=True)
        self.assertEqual(self.m.preferred_source, 'ocr')
        self.assertIsNone(self.m.okx_retry_at)
        self.assertIsNot(self.m.engine, engine)
        self.assertEqual(list(self.m.engine.history), [])
        self.m.on_result(self.m.generation, (100.1, 160., False))
        self.assertIsNone(self.m.engine.percent)
        ocr_engine = self.m.engine
        self.pet.config.data_source = 'okx'
        self.m.set_active(True, preserve_history=True)
        self.assertIsNot(self.m.engine, ocr_engine)
        self.assertEqual(list(self.m.engine.history), [])

    def test_same_api_instrument_retry_after_slow_failures_keeps_history(self):
        engine, history = self.seed_window(82800.)
        with patch('fx_monitor.time.monotonic', return_value=165.):
            self.fail(2)
            self.m.on_okx_result(self.m.generation, Snapshot(82882.8))
        self.assertIs(self.m.engine, engine)
        self.assertEqual(self.m.source, 'okx')
        self.assertAlmostEqual(engine.percent, .1)
        self.assertEqual(self.m.okx_failures, 0)

    def test_source_specific_intervals_and_account_cadence(self):
        self.assertEqual(self.m.timer.interval(), 2000)
        self.assertEqual(self.m.account_timer.interval(), 5000)
        self.fail(3)
        self.assertEqual(self.m.timer.interval(), 3000)
        self.run_retry(self.m.okx_retry_at, Snapshot(100.))
        self.assertEqual(self.m.timer.interval(), 2000)
        self.assertEqual(self.m.engine.policy['interval_ms'], 2000)

    def test_account_failure_does_not_affect_ticker_retry_or_history(self):
        engine, history = self.seed_window()
        for _ in range(4):
            self.m.account_context = (self.m.price_scope(), engine)
            self.m.on_account_result(self.m.generation, None)
        self.assertEqual(self.m.source, 'okx')
        self.assertEqual(self.m.okx_failures, 0)
        self.assertEqual(list(engine.history), history)

    def test_account_risk_is_immediate_and_not_cleared_by_public_ticks(self):
        self.m.account_context = (self.m.price_scope(), self.m.engine)
        updates = []
        self.m.reading.connect(updates.append)
        self.m.on_account_result(self.m.generation,
                                Snapshot(equity=1000., worst_upl_percent=-4.,
                                         account_scope='fixture', private_enabled=True))
        self.assertEqual(self.m.engine.mood, 'panic')
        self.assertIsNone(self.m.engine.percent)
        self.assertIn('止损', updates[-1].text)
        self.assertIn(updates[-1].pose, [13,8,14])
        self.m.on_okx_result(self.m.generation, Snapshot(100.))
        self.assertEqual(self.m.engine.account_risk, 'position')
        self.assertEqual(self.m.engine.mood, 'panic')
        self.assertIn('持仓浮盈亏', self.m.account_status)

    def test_stale_account_result_rejected_after_cross_source_roundtrip(self):
        self.m.account_context = (self.m.price_scope(), self.m.engine)
        self.m.change_source('ocr')
        self.m.change_source('okx')
        self.m.on_account_result(self.m.generation, Snapshot(equity=1000.,
                                worst_upl_percent=-4., account_scope='old', private_enabled=True))
        self.assertIsNone(self.m.engine.account_risk)
        self.assertEqual(self.m.engine.mood,'waiting')

    def test_bad_local_credentials_do_not_break_public_client(self):
        self.m.client = None
        with patch('fx_monitor.load_credentials', side_effect=ValueError('synthetic bad file')) as load:
            self.m.ensure_okx_client()
            load.assert_not_called()
            self.m.capture_account()
        self.assertIsNotNone(self.m.client)
        self.assertFalse(self.m.account_busy)
        self.assertEqual(self.m.okx_failures,0)

    def test_slow_account_worker_does_not_block_public_job_even_with_one_global_worker(self):
        started, release = threading.Event(), threading.Event()
        def slow_account():
            started.set()
            release.wait(3.)
            return Snapshot()
        self.m.account_client = SimpleNamespace(poll_account=slow_account)
        self.m.client.poll_ticker.return_value = Snapshot(100.1)
        pool = QThreadPool.globalInstance()
        old_count = pool.maxThreadCount()
        pool.setMaxThreadCount(1)
        try:
            self.m.capture_account()
            self.assertTrue(started.wait(1.))
            self.assertTrue(self.m.account_busy)
            self.m.capture()
            loop, timer, deadline = QEventLoop(), QTimer(), QTimer()
            timer.timeout.connect(lambda: loop.quit() if self.m.last_price is not None else None)
            deadline.setSingleShot(True)
            deadline.timeout.connect(loop.quit)
            timer.start(10)
            deadline.start(1000)
            loop.exec()
            timer.stop(); deadline.stop()
            self.assertEqual(self.m.last_price,100.1)
            self.assertTrue(self.m.account_busy)
            self.assertFalse(self.m.busy)
        finally:
            release.set()
            self.m.account_pool.waitForDone(2000)
            self.app.processEvents()
            pool.setMaxThreadCount(old_count)

    def test_same_api_instrument_restart_preserves_quotes_and_session(self):
        engine, history = self.seed_window(82800.)
        engine.last_pose_at, engine.rest_done = 115., True
        self.m.set_active(True, preserve_history=True)
        self.assertIs(self.m.engine, engine)
        self.assertEqual(list(engine.history), history)
        self.assertEqual(engine.last_pose_at, 115.)
        self.assertTrue(engine.rest_done)

    def test_different_api_instrument_or_market_never_preserves_quotes(self):
        for key, value in [('inst_id', 'ETH-USDT'), ('simulated', True)]:
            with self.subTest(key=key):
                engine, _ = self.seed_window(82800.)
                self.pet.config.okx[key] = value
                self.m.set_active(True, preserve_history=True)
                self.assertIsNot(self.m.engine, engine)
                self.assertEqual(list(self.m.engine.history), [])
                self.assertIsNone(self.m.engine.percent)

    def test_api_instrument_change_is_checked_again_when_accepting_quote(self):
        engine, _ = self.seed_window(150.)
        self.pet.config.okx['inst_id'] = 'ETH-USDT'
        with patch('fx_monitor.time.monotonic', return_value=130.):
            self.m.on_okx_result(self.m.generation, Snapshot(3000.))
        self.assertIsNot(self.m.engine, engine)
        self.assertEqual(list(self.m.engine.history), [(130., 3000.)])
        self.assertIsNone(self.m.engine.percent)

    def test_recovered_api_warms_up_using_only_new_api_quotes(self):
        self.fail(3)
        self.seed_window(150.)
        self.m.okx_retry_at = 150.
        self.run_retry(150., Snapshot(82800.))
        for elapsed in range(5, 121, 5):
            with patch('fx_monitor.time.monotonic', return_value=150.+elapsed):
                self.m.on_okx_result(self.m.generation, Snapshot(82882.8))
            if elapsed < 10:
                self.assertIsNone(self.m.engine.percent)
            if elapsed < 120:
                self.assertIsNone(self.m.engine.long_percent)
        self.assertAlmostEqual(self.m.engine.long_percent, .1)
        self.assertEqual(self.m.engine.percent, 0.)
        self.assertTrue(all(price >= 82800. for _, price in self.m.engine.history))

    def test_recovered_api_never_compares_ocr_prices_even_similar_scale(self):
        for price in (82800., 150.1):
            with self.subTest(api_price=price):
                self.m.set_active(True)
                self.fail(3)
                ocr_engine, _ = self.seed_window(150.)
                with patch('fx_monitor.time.monotonic', return_value=150.):
                    self.m.okx_retry_at = 150.
                    with patch.object(self.m, 'publish') as publish:
                        self.run_retry(150., Snapshot(price))
                self.assertIsNot(self.m.engine, ocr_engine)
                self.assertEqual(list(self.m.engine.history), [(150., price)])
                self.assertIsNone(self.m.engine.percent)
                update = publish.call_args.args[0]
                self.assertEqual(update.mood, 'waiting')
                self.assertIsNone(update.pose)
                self.assertIsNone(update.text)

    def test_fallback_discards_api_quotes_even_if_ocr_price_is_similar(self):
        for price in (150., 82800.1):
            with self.subTest(ocr_price=price):
                self.m.set_active(True)
                api_engine, _ = self.seed_window(82800.)
                api_engine.last_pose_at, api_engine.rest_done = 115., True
                self.fail(3)
                self.assertIsNot(self.m.engine, api_engine)
                self.assertEqual(list(self.m.engine.history), [])
                self.assertEqual(self.m.engine.last_pose_at, 115.)
                self.assertTrue(self.m.engine.rest_done)
                with patch.object(self.m, 'publish') as publish:
                    self.m.on_result(self.m.generation, (price, 130., False))
                self.assertEqual(list(self.m.engine.history), [(130., price)])
                update = publish.call_args.args[0]
                self.assertIsNone(update.percent)
                self.assertIsNone(update.pose)
                self.assertEqual(update.mood, 'waiting')

    def test_switch_while_request_in_flight_rejects_old_result(self):
        generation = self.m.generation
        self.m.busy = True
        self.m.pending_sample = self.m.sample_context()
        self.pet.config.data_source = 'ocr'
        self.m.set_active(True, preserve_history=True)
        self.m.on_okx_result(generation, Snapshot(999.))
        self.assertIsNone(self.m.last_price)
        self.assertFalse(self.m.busy)
        self.assertEqual(self.m.source, 'ocr')

    def use_ocr(self, rect=None):
        self.pet.config.data_source = 'ocr'
        self.m.set_active(True)
        self.m.set_region({'screen': 'fixture', 'geometry': [0, 0, 1000, 1000],
                           'dpr': 1., 'rect': rect or [10, 20, 200, 60]})

    def test_old_shared_day_is_ignored_and_same_roi_new_symbol_rebases(self):
        self.use_ocr()
        self.pet.settings.setValue('fx/day', json.dumps({
            'date': date.today().isoformat(), 'first': 151., 'last': 151.}))
        self.m.record_price(151.)
        self.assertTrue(self.m.record_price(82650.))
        self.assertIn('持平0.00%', self.m.summary())
        self.assertFalse(self.m.record_price(82732.65))
        self.assertIn('上涨0.10%', self.m.summary())
        self.assertNotIn('54585', self.m.summary())

    def test_scale_jump_down_also_resets_but_small_market_move_does_not(self):
        self.use_ocr()
        self.m.record_price(82650.)
        self.m.record_price(78517.5)
        self.assertIn('下跌5.00%', self.m.summary())
        self.assertTrue(self.m.record_price(151.))
        self.assertIn('持平0.00%', self.m.summary())

    def test_different_price_scale_in_fallback_resets_incompatible_window(self):
        self.seed_window()
        self.fail(3)
        self.m.on_result(self.m.generation, (82650., 140., False))
        self.assertEqual(list(self.m.engine.history), [(140., 82650.)])
        self.assertIsNone(self.m.engine.percent)
        self.assertIn('持平0.00%', self.m.summary())

    def test_direct_registry_region_edit_gets_separate_day_and_survives_restart(self):
        self.use_ocr()
        old_region = json.loads(json.dumps(self.m.region))
        old_key = self.m.day_key()
        self.m.record_price(151.)
        changed = {**old_region, 'rect': [210, 20, 200, 60]}
        # Exercise the settings path used by direct registry edits, bypassing UI.
        self.pet.settings.setValue('fx/region', json.dumps(changed))
        self.pet.settings.sync()
        self.m.record_price(82650.)
        self.assertNotEqual(self.m.day_key(), old_key)
        self.assertIn('持平0.00%', self.m.summary())
        self.m.record_price(83476.5)
        self.assertIn('上涨1.00%', self.m.summary())
        restarted = FXMonitor(self.pet)
        self.assertEqual(restarted.day_key(), self.m.day_key())
        self.assertIn('上涨1.00%', restarted.summary())
        self.pet.settings.setValue('fx/region', json.dumps(old_region))
        self.m.refresh_region()
        self.assertEqual(self.m.day_key(), old_key)
        self.assertIn('持平0.00%', self.m.summary())

    def test_region_edited_during_ocr_discards_frame_from_old_region(self):
        self.use_ocr()
        self.m.pending_sample = self.m.sample_context()
        self.pet.settings.setValue('fx/region', json.dumps({**self.m.region, 'rect': [300, 40, 200, 60]}))
        self.m.on_result(self.m.generation, (151., 100., False))
        self.assertIsNone(self.m.last_price)
        self.assertIn('没有读到', self.m.summary())

    def test_menu_reselection_clears_old_and_selected_roi_day_even_same_rectangle(self):
        self.use_ocr()
        self.m.record_price(151.)
        selected = dict(self.m.region)
        # Run the actual PetWindow menu callback using a minimal window fixture.
        window = SimpleNamespace(fx=self.m, selector=None, timer=Mock(),
                                 show_dialogue=Mock(), fx_action=Mock())
        with patch('main.RegionSelector') as selector:
            PetWindow.select_price_region(window)
            callback = selector.call_args.args[0]
            callback(selected)
        self.assertFalse(self.pet.settings.contains(self.m.day_key()))
        self.assertFalse(self.pet.settings.contains('fx/day'))
        self.m.record_price(82650.)
        self.assertIn('持平0.00%', self.m.summary())

    def test_okx_instruments_and_demo_days_are_independent(self):
        self.m.record_price(82650.)
        btc = self.m.day_key()
        self.pet.config.okx['inst_id'] = 'ETH-USDT'
        self.m.record_price(3000.)
        self.assertNotEqual(self.m.day_key(), btc)
        self.assertIn('持平0.00%', self.m.summary())
        self.pet.config.okx['inst_id'] = 'BTC-USDT'
        self.m.record_price(83476.5)
        self.assertIn('上涨1.00%', self.m.summary())
        self.pet.config.okx['simulated'] = True
        self.assertIn('没有读到', self.m.summary())


if __name__ == '__main__': unittest.main(verbosity=2)
