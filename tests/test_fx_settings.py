"""Editable strategy and market selection; isolated configs and fake public lists."""
from copy import deepcopy
import json
from io import BytesIO
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QLocale, QObject, QSettings, QTimer, Signal
from PySide6.QtWidgets import QApplication, QComboBox, QDialogButtonBox, QPushButton
from fx_logic import (PriceMoodEngine, SENSITIVITY_PRESETS, sensitivity_name,
                      sensitivity_values, validated_policy)
from fx_settings import SensitivityControls
from main import PetWindow
from okx_api import (DEFAULT_OKX, INSTRUMENTS, Credentials, OKXClient, OKXError,
                     Snapshot, validate_options)
from okx_instruments import (CACHE_SECONDS, CatalogJob, InstrumentCatalog,
                             InstrumentSelector, available_instruments, normalize_instrument)
from pet_core import load_config, save_options


class FakeCatalog(QObject):
    finished = Signal(str, bool, object, bool)

    def __init__(self):
        super().__init__()
        self.calls = []

    def request(self, *args):
        self.calls.append(args)


class SensitivityTests(unittest.TestCase):
    def test_presets_and_exit_ratios(self):
        for name, (_, flat, strong, panic) in SENSITIVITY_PRESETS.items():
            policy = validated_policy(sensitivity_values(flat, strong, panic))
            self.assertEqual(sensitivity_name(policy), name)
            self.assertAlmostEqual(policy['flat_exit_percent'], flat / 2)
            self.assertAlmostEqual(policy['strong_exit_percent'], strong * 2 / 3)
        self.assertEqual(validated_policy()['sensitivity_preset'], 'default')

    def test_close_custom_thresholds_keep_exit_order(self):
        policy = validated_policy(sensitivity_values(.29, .30, 3))
        self.assertAlmostEqual(policy['strong_exit_percent'], .295)
        self.assertLess(policy['flat_percent'], policy['strong_exit_percent'])
        self.assertLess(policy['strong_exit_percent'], policy['strong_percent'])

    def test_invalid_thresholds_and_metadata_never_override_numbers(self):
        for values in [(0, 1, 2), (-1, 1, 2), (1, 1, 2), (1, 2, 2),
                       (2, 1, 3), (math.nan, 1, 2), (1, math.inf, 3), (True, 2, 3)]:
            with self.assertRaises(ValueError):
                sensitivity_values(*values)
        policy = validated_policy({**sensitivity_values(.15, .6, 5), 'sensitivity_preset': 'default'})
        self.assertEqual(policy['sensitivity_preset'], 'crypto')

    def test_old_custom_exit_thresholds_are_preserved(self):
        policy = validated_policy({'flat_exit_percent': .05, 'strong_exit_percent': .22})
        self.assertEqual(policy['sensitivity_preset'], 'custom')
        self.assertEqual(policy['flat_exit_percent'], .05)
        self.assertEqual(policy['strong_exit_percent'], .22)

    def test_hot_update_keeps_history_and_requires_two_fresh_confirmations(self):
        engine = PriceMoodEngine(validated_policy())
        for now in range(0, 31, 2):
            engine.receive(100., now)
        engine.receive(100.1, 32.)
        self.assertEqual((engine.pending_mood, engine.pending_count), ('up', 1))
        history, rows = engine.history, list(engine.history)
        started, last_pose, rest = engine.started, engine.last_pose_at, engine.rest_done
        engine.apply_policy(validated_policy(sensitivity_values(.05, .2, 2)), 32.)
        self.assertIs(engine.history, history)
        self.assertEqual(list(history), rows)
        self.assertEqual((engine.pending_mood, engine.pending_count), (None, 0))
        self.assertEqual((engine.started, engine.last_pose_at, engine.rest_done), (started, last_pose, rest))
        self.assertEqual(engine.receive(100.1, 34.).mood, 'flat')
        self.assertEqual(engine.pending_count, 1)
        self.assertEqual(engine.receive(100.1, 36.).mood, 'up')

    def test_new_panic_threshold_rechecks_existing_return_immediately(self):
        engine = PriceMoodEngine(validated_policy())
        for now in range(0, 31, 2):
            engine.receive(100., now)
        engine.receive(97.5, 32.)
        engine.last_pose_at = 32.
        rows = list(engine.history)
        result = engine.apply_policy(validated_policy(sensitivity_values(.05, .2, 2)), 33.)
        self.assertEqual(result.mood, 'panic')
        self.assertIn(result.pose, (13, 8, 14))
        self.assertEqual(list(engine.history), rows)
        self.assertEqual(engine.pending_count, 0)


class InstrumentTests(unittest.TestCase):
    def test_normalization_and_suffix_exactly_once(self):
        self.assertEqual(normalize_instrument(' btc-usdt ', 'SPOT'), 'BTC-USDT')
        self.assertEqual(normalize_instrument('btc-usdt', 'SWAP'), 'BTC-USDT-SWAP')
        self.assertEqual(normalize_instrument('BTC-USDT-SWAP', 'SWAP'), 'BTC-USDT-SWAP')
        for value, kind in [('BTC', 'SPOT'), ('BTC-USDT-SWAP', 'SPOT'),
                            ('BTC-USDT-SWAP-SWAP', 'SWAP'), ('BTC/USDT', 'SPOT'),
                            ('BTC-USDT', 'FUTURES')]:
            with self.assertRaises(ValueError):
                normalize_instrument(value, kind)

    def test_old_market_type_is_inferred_from_id(self):
        for value, kind in [('IRYS-USDT-SWAP', 'SWAP'), ('ETH-USDT', 'SPOT')]:
            options = validate_options({'inst_id': value})
            self.assertEqual(options['inst_id'], value)
            self.assertEqual(options['inst_type'], kind)
        self.assertEqual(validate_options({'inst_id': 'IRYS-USDT-SWAP', 'inst_type': 'SPOT'})['inst_type'], 'SWAP')

    def test_catalog_filters_malformed_inactive_and_wrong_market(self):
        rows = [{'instId': 'BTC-USDT', 'instType': 'SPOT', 'state': 'live'},
                {'instId': 'ETH-USDT', 'instType': 'SPOT', 'state': 'suspend'},
                {'instId': 'BTC-USD-SWAP', 'instType': 'SWAP', 'state': 'live'},
                {'instId': 'BAD ID', 'instType': 'SPOT', 'state': 'live'}, {}, None]
        self.assertEqual(available_instruments(rows * 2, 'SPOT'), ['BTC-USDT'])
        self.assertEqual(available_instruments(rows, 'SWAP'), ['BTC-USD-SWAP'])

    def test_public_catalog_is_unsigned_even_when_client_has_keys(self):
        calls = []
        def transport(path, headers, timeout):
            calls.append((path, headers))
            return {'code': '0', 'data': []}
        client = OKXClient(DEFAULT_OKX, Credentials('fake-key', 'fake-secret', 'fake-pass'), transport)
        self.assertEqual(client.instruments('SWAP'), [])
        self.assertEqual(calls[0][0], INSTRUMENTS + '?instType=SWAP')
        self.assertFalse(any('ACCESS' in key for key in calls[0][1]))
        for private, query in [(True, {'instType': 'SPOT'}), (False, {'instType': 'FUTURES'}),
                               (False, {'instType': 'SPOT', 'instId': 'BTC-USDT'}), (False, None)]:
            with self.assertRaises(OKXError):
                client._get(INSTRUMENTS, query, private=private)
        self.assertEqual(len(calls), 1)

    def test_catalog_worker_uses_public_client_and_sanitizes_failure(self):
        for failed in (False, True):
            job = CatalogJob('SWAP', True)
            rows = []
            job.signals.done.connect(lambda *args: rows.append(args))
            with patch('okx_instruments.OKXClient') as client:
                if failed:
                    client.return_value.instruments.side_effect = RuntimeError('server-private-text')
                else:
                    client.return_value.instruments.return_value = [
                        {'instId': 'BTC-USDT-SWAP', 'instType': 'SWAP', 'state': 'live'}]
                job.run()
                self.assertEqual(client.call_args.kwargs, {})
                self.assertEqual(client.call_args.args[0]['simulated'], True)
                self.assertEqual(len(client.call_args.args), 1)
            self.assertEqual(rows, [('SWAP', True, None if failed else ['BTC-USDT-SWAP'])])

    def test_full_catalog_allows_more_than_one_megabyte_but_ticker_does_not(self):
        from okx_api import ORIGIN, TICKER
        payload = json.dumps({'code': '0', 'data': [{'padding': 'x' * (1024 * 1024)}]}).encode()
        class Response(BytesIO):
            def geturl(self):
                return url
        client = OKXClient(DEFAULT_OKX)
        client.opener = Mock()
        client.opener.open.side_effect = lambda *args, **kwargs: Response(payload)
        url = ORIGIN + INSTRUMENTS + '?instType=SPOT'
        self.assertEqual(len(client.instruments('SPOT')), 1)
        url = ORIGIN + TICKER + '?instId=BTC-USDT'
        with self.assertRaises(OKXError):
            client.poll_ticker()


class WidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_custom_thresholds_show_at_most_two_decimals_without_trailing_zeros(self):
        widget = SensitivityControls(validated_policy())
        widget.preset.setCurrentIndex(widget.preset.findData('custom'))
        for spin in widget.spins:
            spin.setLocale(QLocale('en_US'))
            self.assertEqual(spin.decimals(), 2)
        self.assertEqual([spin.text() for spin in widget.spins], ['0.08 %', '0.3 %', '3 %'])
        widget.spins[0].setValue(.5)
        self.assertEqual(widget.spins[0].text(), '0.5 %')
        for spin, value in zip(widget.spins, (.086, .307, 3.127)):
            spin.setValue(value)
        policy = widget.policy()
        self.assertEqual([policy[key] for key in ('flat_percent', 'strong_percent', 'panic_drop_percent')],
                         [.09, .31, 3.13])
        widget.spins[1].setLocale(QLocale('de_DE'))
        self.assertEqual(widget.spins[1].text(), '0,31 %')
        widget.spins[1].setValue(.3)
        self.assertEqual(widget.spins[1].text(), '0,3 %')
        widget.deleteLater()

    def test_custom_validation_restore_and_untouched_legacy_values(self):
        policy = validated_policy({'flat_exit_percent': .05, 'strong_exit_percent': .22})
        widget = SensitivityControls(policy)
        self.assertEqual(widget.preset.currentData(), 'custom')
        self.assertEqual(widget.policy(), policy)
        widget.spins[0].setValue(.4)
        self.assertFalse(widget.valid)
        with self.assertRaises(ValueError):
            widget.policy()
        widget.restore_default()
        self.assertTrue(widget.valid)
        self.assertEqual(widget.policy()['sensitivity_preset'], 'default')
        self.assertTrue(widget.custom.isHidden())
        widget.deleteLater()

    def test_ocr_selector_is_disabled_and_never_fetches_or_changes_id(self):
        catalog = FakeCatalog()
        widget = InstrumentSelector(validate_options({'inst_id': 'IRYS-USDT-SWAP'}), False, catalog=catalog)
        self.assertFalse(widget.controls.isEnabled())
        widget.load(True)
        widget.instrument.setEditText('ETH-USDT')
        self.assertEqual(widget.selection(), ('IRYS-USDT-SWAP', 'SWAP'))
        self.assertEqual(catalog.calls, [])
        widget.deleteLater()

    def test_search_manual_entry_market_switch_and_stale_lists(self):
        catalog = FakeCatalog()
        widget = InstrumentSelector(validate_options({'inst_id': 'IRYS-USDT-SWAP'}), True, catalog=catalog)
        self.assertEqual(widget.selection(), ('IRYS-USDT-SWAP', 'SWAP'))
        widget.load()
        self.assertEqual(catalog.calls[-1], ('SWAP', False, False))
        widget._loaded('SWAP', False, ['BTC-USDT-SWAP', 'ETH-USDT-SWAP'], False)
        self.assertEqual(widget.instrument.currentText(), 'IRYS-USDT')
        widget.instrument.setEditText('btc-usdt-swap')
        self.assertEqual(widget.selection(), ('BTC-USDT-SWAP', 'SWAP'))
        widget.market.setCurrentIndex(widget.market.findData('SPOT'))
        self.assertEqual(widget.selection(), ('BTC-USDT', 'SPOT'))
        widget.instrument.setEditText('eth-usdt')
        widget._loaded('SWAP', False, ['BAD-USDT-SWAP'], False)
        self.assertEqual(widget.instrument.currentText(), 'eth-usdt')
        widget._loaded('SPOT', False, [], True)
        self.assertEqual(widget.selection(), ('ETH-USDT', 'SPOT'))
        self.assertIn('失败', widget.status.text())
        self.assertEqual(widget.instrument.completer().filterMode().name, 'MatchContains')
        widget._loaded('SPOT', False, ['BTC-USDT', 'ETH-USDT', 'BTC-USD'], False)
        widget.instrument.completer().setCompletionPrefix('btc')
        self.assertEqual(widget.instrument.completer().completionCount(), 2)
        widget.set_simulated(True)
        widget._loaded('SPOT', False, ['OLD-USDT'], False)
        self.assertEqual(widget.instrument.currentText(), 'eth-usdt')
        widget.deleteLater()

    def test_cached_catalog_requests_are_coalesced_and_failure_keeps_cache(self):
        catalog = InstrumentCatalog()
        pool = Mock()
        catalog.pool = pool
        records = []
        catalog.finished.connect(lambda *args: records.append(args))
        with patch('okx_instruments.time.monotonic', return_value=100.):
            catalog.request('SPOT')
            catalog.request('SPOT')
            self.assertEqual(pool.start.call_count, 1)
            catalog._received('SPOT', False, ['BTC-USDT'])
            catalog.request('SPOT')
            self.assertEqual(pool.start.call_count, 1)
            catalog.request('SPOT', force=True)
            self.assertEqual(pool.start.call_count, 2)
            catalog._received('SPOT', False, None)
            self.assertEqual(records[-1], ('SPOT', False, ['BTC-USDT'], True))
        with patch('okx_instruments.time.monotonic', return_value=101. + CACHE_SECONDS):
            catalog.request('SPOT')
            self.assertEqual(pool.start.call_count, 3)
        catalog.deleteLater()


class SettingsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'build')
        self.folder = Path(self.temp.name)
        data = json.loads((ROOT / 'kurumi.json').read_text(encoding='utf-8-sig'))
        data['data_source'] = 'okx'
        data['okx'] = {**DEFAULT_OKX, 'inst_id': 'IRYS-USDT-SWAP'}
        data['fx']['custom_extension'] = 'keep-me'
        data['okx']['custom_extension'] = 'keep-too'
        (self.folder / 'kurumi.json').write_text(json.dumps(data), encoding='utf-8')
        self.key_path = self.folder / 'okx.local.json'
        self.key_path.write_text('{"api_key":"fake-key", "secret_key":"fake-secret", "passphrase":"fake-pass"}', encoding='utf-8')
        self.key_bytes = self.key_path.read_bytes()
        settings = QSettings(str(self.folder / 'isolated.ini'), QSettings.Format.IniFormat)
        self.pet = PetWindow(external=self.folder, settings=settings, tray=False)
        self.pet.audio.set_muted(True)
        self.warnings = []
        self.warning_patch = patch.object(self.pet, 'show_warning', side_effect=lambda text, *args: self.warnings.append(text))
        self.warning_patch.start()

    def tearDown(self):
        self.pet.fx.set_active(False)
        self.pet.close()
        self.pet.deleteLater()
        self.app.processEvents()
        self.warning_patch.stop()
        self.temp.cleanup()

    def open_settings(self, edit):
        errors = []
        def inspect():
            dialog = self.app.activeModalWidget()
            try:
                self.assertIsNotNone(dialog)
                edit(dialog)
            except Exception as error:
                errors.append(error)
            finally:
                if dialog and dialog.isVisible():
                    dialog.reject()
        QTimer.singleShot(0, inspect)
        self.pet.fx_settings(load_instruments=False)
        if errors:
            raise errors[0]

    def start_fake_monitor(self, source='okx'):
        monitor = self.pet.fx
        monitor.source = source
        monitor.preferred_source = self.pet.config.data_source
        monitor.active = True
        monitor.reset_price_window()
        for now in range(0, 31, 2):
            monitor.engine.receive(100., now)
        monitor.engine.receive(100.1, 32.)
        return monitor

    def test_save_sensitivity_keeps_window_resets_confirmation_and_preserves_keys_id(self):
        monitor = self.start_fake_monitor()
        engine, history = monitor.engine, list(monitor.engine.history)
        self.assertEqual(engine.pending_count, 1)
        generation = monitor.generation
        def edit(dialog):
            preset = dialog.findChild(QComboBox, 'fx_sensitivity')
            preset.setCurrentIndex(preset.findData('forex_stock'))
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
        self.open_settings(edit)
        self.assertEqual(self.warnings, [])
        self.assertIs(monitor.engine, engine)
        self.assertEqual(list(engine.history), history)
        self.assertEqual(engine.pending_count, 0)
        self.assertEqual(monitor.generation, generation)
        self.assertEqual(engine.policy['flat_percent'], .05)
        self.assertEqual(self.pet.config.okx['inst_id'], 'IRYS-USDT-SWAP')
        self.assertEqual(self.key_path.read_bytes(), self.key_bytes)
        saved = json.loads((self.folder / 'kurumi.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['fx']['sensitivity_preset'], 'forex_stock')
        self.assertEqual(saved['fx']['custom_extension'], 'keep-me')
        self.assertEqual(saved['okx']['custom_extension'], 'keep-too')
        self.assertNotIn('fake-secret', json.dumps(saved))
        self.assertEqual(load_config(self.folder, ROOT).fx['flat_percent'], .05)

    def test_invalid_custom_disables_save_and_cancel_keeps_config(self):
        before = (self.folder / 'kurumi.json').read_bytes()
        def edit(dialog):
            controls = dialog.findChild(SensitivityControls)
            controls.preset.setCurrentIndex(controls.preset.findData('custom'))
            controls.spins[0].setValue(.7)
            self.assertFalse(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).isEnabled())
            self.assertFalse(dialog.findChild(QPushButton, 'fx_sensitivity_restore') is None)
            controls.restore_default()
            self.assertTrue(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).isEnabled())
            dialog.reject()
        self.open_settings(edit)
        self.assertEqual((self.folder / 'kurumi.json').read_bytes(), before)
        self.assertEqual(self.key_path.read_bytes(), self.key_bytes)

    def test_custom_threshold_save_uses_two_decimal_input_precision(self):
        def edit(dialog):
            controls = dialog.findChild(SensitivityControls)
            controls.preset.setCurrentIndex(controls.preset.findData('custom'))
            for spin, value in zip(controls.spins, (.086, .307, 3.127)):
                spin.setValue(value)
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
        self.open_settings(edit)
        self.assertEqual(self.warnings, [])
        saved = json.loads((self.folder / 'kurumi.json').read_text(encoding='utf-8'))
        self.assertEqual([saved['fx'][key] for key in ('flat_percent', 'strong_percent', 'panic_drop_percent')],
                         [.09, .31, 3.13])
        self.assertEqual(saved['okx']['inst_id'], 'IRYS-USDT-SWAP')
        self.assertEqual(self.key_path.read_bytes(), self.key_bytes)

    def test_changed_pair_writes_inst_id_rewarms_keeps_session_and_discards_old_quote(self):
        monitor = self.start_fake_monitor()
        engine, generation, started = monitor.engine, monitor.generation, monitor.engine.started
        self.pet.settings.setValue('fx/stop_loss', '98')
        def edit(dialog):
            dialog.findChild(QComboBox, 'okx_inst_id').setEditText('ETH-USDT')
            with patch('fx_monitor.QTimer.singleShot'):
                dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
        self.open_settings(edit)
        monitor.timer.stop()
        monitor.account_timer.stop()
        monitor.rest_timer.stop()
        self.assertEqual(self.pet.config.okx['inst_id'], 'ETH-USDT-SWAP')
        self.assertIsNot(monitor.engine, engine)
        self.assertFalse(monitor.engine.history)
        self.assertEqual(monitor.engine.started, started)
        monitor.on_okx_result(generation, Snapshot(82800.))
        self.assertFalse(monitor.engine.history)
        self.assertIsNone(monitor.stop_loss())
        self.assertEqual(self.key_path.read_bytes(), self.key_bytes)

    def test_ocr_mode_sensitivity_keeps_ocr_window_and_does_not_change_inst_id(self):
        self.pet.config.data_source = 'ocr'
        monitor = self.start_fake_monitor('ocr')
        engine, history = monitor.engine, list(monitor.engine.history)
        def edit(dialog):
            selector = dialog.findChild(InstrumentSelector)
            self.assertFalse(selector.controls.isEnabled())
            selector.instrument.setEditText('BTC-USDT')
            controls = dialog.findChild(SensitivityControls)
            controls.preset.setCurrentIndex(controls.preset.findData('crypto'))
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
        self.open_settings(edit)
        self.assertEqual(self.warnings, [])
        self.assertIs(monitor.engine, engine)
        self.assertEqual(list(engine.history), history)
        self.assertEqual(monitor.source, 'ocr')
        self.assertEqual(self.pet.config.okx['inst_id'], 'IRYS-USDT-SWAP')
        self.assertEqual(engine.policy['flat_percent'], .15)

    def test_unchanged_save_preserves_custom_exits_and_does_not_rearm_stop(self):
        monitor = self.start_fake_monitor()
        custom = validated_policy({**self.pet.config.fx, 'flat_exit_percent': .05,
                                   'strong_exit_percent': .22})
        self.pet.config.fx = custom
        monitor.policy = custom
        monitor.engine.policy = deepcopy(custom)
        self.pet.settings.setValue('fx/stop_loss', '101')
        monitor.engine.stop_loss = 101.
        monitor.engine.below_stop = True
        engine = monitor.engine
        def edit(dialog):
            self.assertEqual(dialog.findChild(SensitivityControls).preset.currentData(), 'custom')
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
        self.open_settings(edit)
        self.assertIs(monitor.engine, engine)
        self.assertTrue(engine.below_stop)
        self.assertEqual(self.pet.config.fx['strong_exit_percent'], .22)
        self.assertEqual(self.key_path.read_bytes(), self.key_bytes)

    def test_change_pair_and_sensitivity_together_never_uses_old_pair_return(self):
        monitor = self.start_fake_monitor()
        monitor.engine.percent = -2.5
        readings = []
        monitor.reading.connect(readings.append)
        def edit(dialog):
            dialog.findChild(QComboBox, 'okx_inst_id').setEditText('ETH-USDT')
            controls = dialog.findChild(SensitivityControls)
            controls.preset.setCurrentIndex(controls.preset.findData('forex_stock'))
            with patch('fx_monitor.QTimer.singleShot'):
                dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
        self.open_settings(edit)
        monitor.timer.stop()
        monitor.account_timer.stop()
        monitor.rest_timer.stop()
        self.assertFalse(monitor.engine.history)
        self.assertEqual(monitor.engine.pending_count, 0)
        self.assertEqual(monitor.engine.mood, 'waiting')
        self.assertEqual(readings, [])

    def test_changing_api_settings_during_fallback_keeps_ocr_window(self):
        monitor = self.start_fake_monitor('ocr')
        engine, rows, generation = monitor.engine, list(monitor.engine.history), monitor.generation
        def edit(dialog):
            dialog.findChild(QComboBox, 'okx_inst_id').setEditText('ETH-USDT')
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
        self.open_settings(edit)
        self.assertIs(monitor.engine, engine)
        self.assertEqual(list(engine.history), rows)
        self.assertGreater(monitor.generation, generation)
        self.assertIsNotNone(monitor.okx_retry_at)
        self.assertEqual(self.pet.config.okx['inst_id'], 'ETH-USDT-SWAP')


if __name__ == '__main__':
    unittest.main(verbosity=2)
