"""Headless verification for the frozen binary: Kurumi.exe --self-test report.json."""
import json
from pathlib import Path

from PySide6.QtCore import QEventLoop, QSettings, QTimer, qVersion
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QRegion
from PySide6.QtWidgets import QApplication, QWidget


def run(report_path, external, bundled):
    from main import PetWindow
    report_path = Path(report_path).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path = report_path.with_suffix('.ini')
    settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    pet = PetWindow(external=external, bundled=bundled, settings=settings, tray=False)
    pet.audio.set_muted(True)
    pet.show()
    QApplication.processEvents()
    # Open the actual settings dialog with an empty, isolated credential folder.
    # Native Windows frame margins must not undo its initial placement.
    import tempfile
    original_external, original_position = pet.external, pet.pos()
    placement_checks = []
    settings_checks = []
    area = pet.screen().availableGeometry()
    pet.move(area.center()-pet.rect().center())
    stationary = pet.pos()

    def inspect_settings():
        dialog = QApplication.activeModalWidget()
        try:
            placement_checks.append(bool(dialog and not dialog.frameGeometry().intersects(pet.frameGeometry())
                                         and pet.pos() == stationary
                                         and any(screen.availableGeometry().contains(dialog.frameGeometry())
                                                 for screen in QApplication.screens())))
            from fx_settings import SensitivityControls
            from okx_instruments import InstrumentSelector
            from PySide6.QtCore import QPoint, QPointF, Qt
            from PySide6.QtGui import QWheelEvent
            from PySide6.QtWidgets import QDialogButtonBox, QDoubleSpinBox, QLineEdit, QPushButton
            controls = dialog.findChild(SensitivityControls)
            selector = dialog.findChild(InstrumentSelector)
            if (controls is None or selector is None
                    or selector.selection()[0] != pet.config.okx['inst_id']
                    or selector.controls.isEnabled() != (pet.config.data_source == 'okx')):
                raise RuntimeError('Packaged strategy/market widgets missing or changed existing instrument')
            controls.preset.setCurrentIndex(controls.preset.findData('custom'))
            controls.spins[0].setValue(1.)
            if dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).isEnabled():
                raise RuntimeError('Packaged threshold validation did not block Save')
            controls.restore_default()
            if not controls.valid:
                raise RuntimeError('Packaged restore sensitivity failed')
            controls.preset.setCurrentIndex(controls.preset.findData('custom'))
            spins = dialog.findChildren(QDoubleSpinBox)
            if len(spins) != 5:
                raise RuntimeError('Packaged numeric controls missing')
            for spin in spins:
                spin.setFocus()
                before = spin.value()
                for target in (spin, spin.lineEdit()):
                    event = QWheelEvent(QPointF(5, 5), QPointF(target.mapToGlobal(QPoint(5, 5))),
                                        QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
                    QApplication.sendEvent(target, event)
                    if spin.value() != before:
                        raise RuntimeError('Packaged numeric wheel changed a setting')
            dialog.findChild(QDoubleSpinBox, 'okx_position_stop_loss_percent').setValue(1.01)
            dialog.findChild(QDoubleSpinBox, 'okx_daily_loss_percent').setValue(2.)
            dialog.findChild(QLineEdit, 'fx_stop_loss').setText('98.5')
            dialog.findChild(QPushButton, 'fx_restore_numeric_defaults').click()
            if (controls.policy()['sensitivity_preset'] != 'default'
                    or dialog.findChild(QDoubleSpinBox, 'okx_position_stop_loss_percent').value() != 3.
                    or dialog.findChild(QDoubleSpinBox, 'okx_daily_loss_percent').value() != 3.
                    or dialog.findChild(QLineEdit, 'fx_stop_loss').text()
                    or any('重新框选' in button.text() for button in dialog.findChildren(QPushButton))):
                raise RuntimeError('Packaged restore numeric defaults failed')
            dialog.grab().save(str(report_path.with_name(report_path.stem+'-settings.png')))
            settings_checks.append(True)
        except Exception:
            settings_checks.append(False)
        finally:
            if dialog:
                dialog.reject()

    try:
        with tempfile.TemporaryDirectory(dir=report_path.parent) as empty_folder:
            pet.external = Path(empty_folder)
            QTimer.singleShot(100, inspect_settings)
            pet.fx_settings(load_instruments=False)
        pet.show_permission_warning('测试提示：建议改成只读。')
        QApplication.processEvents()
        placement_checks.append(not pet.permission_box.frameGeometry().intersects(pet.frameGeometry()))
        pet.permission_box.reject()
    finally:
        pet.external = original_external
        pet.move(original_position)
    if placement_checks != [True, True]:
        raise RuntimeError('Native dialog placement overlaps the pet or work area')
    if settings_checks != [True]:
        raise RuntimeError('Packaged settings controls failed validation')
    from PySide6.QtCore import QRect
    from PySide6.QtCore import QPoint
    from fx_region import region_data
    # Match the native logical/global mapping at the current display's DPR.
    # No desktop image is captured: only our own painted coverage is checked.
    blank = QRect(pet.mapToGlobal(QPoint(20, 10)), QRect(0, 0, 60, 20).size())
    if pet.occludes_price_rect(blank):
        raise RuntimeError('Packaged transparent padding falsely blocks OCR')
    painted = QRect(pet.mapToGlobal(pet.IMAGE_BOX.topLeft()), pet.IMAGE_BOX.size())
    if not pet.occludes_price_rect(painted):
        raise RuntimeError('Packaged visible character fails OCR occlusion check')
    if pet.occludes_price_rect(painted.translated(pet.width()+1000, 0)):
        raise RuntimeError('Packaged distant price falsely blocks OCR')
    original_source, original_region = pet.config.data_source, pet.fx.region
    try:
        pet.config.data_source = 'ocr'
        pet.fx.region = region_data(pet.screen(), QRect(10, 10, 100, 30))
        for _ in range(2):
            pet.toggle_fx(True)
            QApplication.processEvents()
            if pet.selector is None or pet.fx.active or not any(overlay.isVisible() for overlay in pet.selector.overlays):
                raise RuntimeError('Packaged OCR start did not request a new region')
            pet.selector.finish(None)
            if pet.fx.active or pet.fx_action.isChecked():
                raise RuntimeError('Packaged OCR cancel reused a previous region')
    finally:
        if pet.selector:
            pet.selector.finish(None)
        pet.config.data_source, pet.fx.region = original_source, original_region
    if not QFontMetrics(pet.font).inFontUcs4(ord('久')):
        raise RuntimeError('Chinese font unavailable')
    for _ in range(50):
        before = pet.current
        pet.react()
        if pet.current == before or pet.text not in pet.config.lines[pet.current]:
            raise RuntimeError('Pose/dialogue validation failed')
    pet.reset_pose()
    if pet.current != pet.config.default or pet.text:
        raise RuntimeError('Default pose validation failed')
    pet.react()
    loop = QEventLoop()
    QTimer.singleShot(350, loop.quit)
    loop.exec()
    ratio = max(1., pet.devicePixelRatioF())
    image = QImage(round(pet.width()*ratio), round(pet.height()*ratio),
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(ratio)
    image.fill(0)
    painter = QPainter(image)
    pet.render(painter, pet.rect().topLeft(), QRegion(pet.rect()),
               QWidget.RenderFlag.DrawWindowBackground | QWidget.RenderFlag.DrawChildren |
               QWidget.RenderFlag.IgnoreMask)
    painter.end()
    if not image.save(str(report_path.with_suffix('.png'))):
        raise RuntimeError('Unable to save rendered preview')
    # Exercise bundled ONNX inference on a generated quote; no real screen capture.
    from fx_monitor import OCRJob
    from fx_logic import PriceMoodEngine
    fixture = QImage(230, 64, QImage.Format.Format_RGB888)
    fixture.fill(QColor('white'))
    fixture_painter = QPainter(fixture)
    fixture_painter.setPen(QColor('black'))
    font = QFont('Arial')
    font.setPixelSize(40)
    fixture_painter.setFont(font)
    fixture_painter.drawText(10, 46, '123.45')
    fixture_painter.end()
    results = []
    job = OCRJob(1, fixture, .90, 0.)
    job.signals.done.connect(lambda generation, result: results.append(result))
    job.run()
    if not results or results[0][0] != 123.45 or results[0][2]:
        raise RuntimeError('Packaged local OCR validation failed')
    engine = PriceMoodEngine(pet.config.fx)
    for t in range(0, 120, 10):
        engine.receive(100., t)
    update = engine.receive(96., 120.)
    if update.pose not in (13, 8, 14) or update.percent != -4.:
        raise RuntimeError('Packaged price policy validation failed')
    if pet.fx.active or pet.fx.timer.interval() != pet.fx.sampling_interval():
        raise RuntimeError('FX must be opt-in with its source-specific interval')
    if (pet.config.fx['interval_ms'], pet.config.fx['okx_interval_ms'],
            pet.config.fx['account_interval_ms']) != (3000, 2000, 5000):
        raise RuntimeError('Packaged responsive sampling intervals failed')
    jitter_engine = PriceMoodEngine(pet.config.fx)
    for t in range(0, 131, 5):
        now = t+.01 if t == 10 else t-.01 if t == 130 else float(t)
        price = 99. if t < 10 else 99.8 if t == 130 else 100.
        jitter_update = jitter_engine.receive(price, now)
    if (jitter_engine.history[0] != (10.01, 100.) or jitter_update.percent != -.2
            or jitter_update.long_percent != -.2):
        raise RuntimeError('Packaged rolling-window jitter regression failed')
    warmup = PriceMoodEngine(pet.config.fx)
    for t in range(0, 14, 2):
        early = warmup.receive(100+t*.02, t)
    if early.pose != 2 or early.mood != 'up' or not early.warming or early.long_percent is not None:
        raise RuntimeError('Packaged short warmup failed')
    if pet.images[10].width() != 1024:
        raise RuntimeError('Original image resolution was not preserved')
    from pet_audio import SOUND_NAMES, WindowsMP3Player, sound_candidates
    player = WindowsMP3Player()
    sound_lengths = {}
    for name in SOUND_NAMES:
        for path in sound_candidates(name, external, bundled):
            if path.is_file():
                length = player.duration(path)
                if length and length > 0:
                    sound_lengths[name] = length
                    break
        else:
            raise RuntimeError(f'Packaged MP3 decode failed: {name}')
    if not (bundled / 'LICENSES.txt').is_file():
        raise RuntimeError('Bundled sound license is missing')
    # A deterministic public API fixture also verifies SSL/signature modules bundle.
    # It never reads credentials or performs a network request.
    from okx_api import OKXClient, DEFAULT_OKX
    import time
    client = OKXClient(DEFAULT_OKX, transport=lambda *args: {
        'code': '0', 'data': [{'instId': 'BTC-USDT', 'last': '123.45', 'ts': str(int(time.time()*1000))}]})
    if client.poll().price != 123.45:
        raise RuntimeError('Packaged OKX public fixture failed')
    from okx_instruments import available_instruments, normalize_instrument
    client.transport = lambda *args: {'code': '0', 'data': [
        {'instId': 'BTC-USDT-SWAP', 'instType': 'SWAP', 'state': 'live'}]}
    if (available_instruments(client.instruments('SWAP'), 'SWAP') != ['BTC-USDT-SWAP']
            or normalize_instrument('btc-usdt-swap', 'SWAP') != 'BTC-USDT-SWAP'):
        raise RuntimeError('Packaged instrument catalog failed')
    client.transport = lambda *args: {
        'code': '0', 'data': [{'instId': 'BTC-USDT', 'last': '123.45', 'ts': str(int(time.time()*1000))}]}
    from fx_logic import sensitivity_values
    strategy = PriceMoodEngine(pet.config.fx)
    for tick in range(0, 31, 2):
        strategy.receive(100., tick)
    strategy.receive(100.1, 32.)
    previous_rows = list(strategy.history)
    strategy.apply_policy({**strategy.policy, **sensitivity_values(.05, .2, 2.)}, 32.)
    if (list(strategy.history) != previous_rows or strategy.pending_count != 0
            or strategy.receive(100.1, 34.).mood != 'flat'
            or strategy.receive(100.1, 36.).mood != 'up'):
        raise RuntimeError('Packaged sensitivity update reused confirmations or cleared quotes')
    # Exercise the real background job, fallback and automatic recovery using
    # only the fixture client. Never activate credential loading or real capture.
    monitor = pet.fx
    monitor.client = client
    monitor.source = monitor.preferred_source = 'okx'
    monitor.active = True
    monitor.reset_price_window()
    now = time.monotonic()
    for age in range(120, -1, -5):
        monitor.engine.receive(123.45, now-age)
    original_engine = monitor.engine
    for count in range(1, 4):
        monitor.on_okx_result(monitor.generation, None)
        if monitor.source != ('ocr' if count == 3 else 'okx'):
            raise RuntimeError('Packaged OKX consecutive failure policy failed')
    if monitor.engine is original_engine or monitor.engine.history or monitor.engine.percent is not None:
        raise RuntimeError('Packaged fallback mixed API and OCR history')
    ocr_engine = monitor.engine
    for age in range(120, -1, -5):
        ocr_engine.receive(150., now-age)
    loop = QEventLoop()
    monitor.source_changed.connect(loop.quit)
    monitor.okx_retry_at = 0.
    monitor.capture()  # API retry takes priority even without a valid OCR region.
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    monitor.source_changed.disconnect(loop.quit)
    if (monitor.source != 'okx' or monitor.busy or monitor.engine is ocr_engine
            or monitor.engine.percent is not None or len(monitor.engine.history) != 1
            or monitor.engine.history[0][1] != 123.45 or monitor.okx_failures != 0):
        raise RuntimeError('Packaged OKX automatic recovery failed')
    monitor.set_active(False)
    monitor.source = 'ocr'
    monitor.set_region({'screen': 'self-test', 'geometry': [0, 0, 1000, 1000],
                        'dpr': 1., 'rect': [10, 20, 100, 30]})
    monitor.record_price(151.)
    monitor.record_price(82650.)
    if '0.00%' not in monitor.summary():
        raise RuntimeError('Packaged daily price-scale reset failed')
    # Drop the old instrument's rolling window just as on_result normally does.
    monitor.reset_price_window()
    monitor.record_price(82732.65)
    if '0.10%' not in monitor.summary():
        raise RuntimeError('Packaged daily summary failed')
    # Exercise the actual Windows flags without capturing the user's screen.
    from fx_logic import validated_policy
    from fx_debug import LOGGER, record
    import ctypes
    import io
    import logging
    import sys
    capture_flags = {}
    previous_excluded = pet.fx.policy['exclude_from_capture']
    try:
        if sys.platform == 'win32':
            api = ctypes.WinDLL('user32', use_last_error=True)
            api.GetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            for excluded in (True, False):
                pet.fx.policy['exclude_from_capture'] = excluded
                pet.fx.apply_capture_policy()
                value = ctypes.c_uint()
                if not api.GetWindowDisplayAffinity(int(pet.winId()), ctypes.byref(value)):
                    raise RuntimeError('Unable to query packaged capture flag')
                capture_flags[str(excluded)] = value.value
                if value.value != (0x11 if excluded else 0):
                    raise RuntimeError('Packaged capture toggle failed')
    finally:
        pet.fx.policy['exclude_from_capture'] = previous_excluded
        pet.fx.capture_affinity(0)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    propagate = LOGGER.propagate
    LOGGER.propagate = False
    LOGGER.addHandler(handler)
    try:
        record(False, 'self_test')
        if stream.getvalue():
            raise RuntimeError('Disabled diagnostics emitted a record')
        record(True, 'self_test', price=123.45, skipped=False)
        if json.loads(stream.getvalue().split('FX_DEBUG ', 1)[1])['price'] != 123.45:
            raise RuntimeError('Packaged diagnostics failed')
    finally:
        LOGGER.removeHandler(handler)
        LOGGER.propagate = propagate
        handler.close()
    defaults = validated_policy()
    result = {'ok': True, 'qt': qVersion(), 'images': len(pet.images),
              'dialogs_avoid_pet': True,
              'fx_fresh_region_each_start': True,
              'settings_wheel_ignored': True,
              'settings_numeric_defaults': True,
              'fx_visible_pixel_occlusion': True,
              'default': pet.config.default, 'duration': pet.config.duration,
              'dpr': ratio, 'source_pixels': pet.images[10].width(),
              'fx_interval': pet.fx.timer.interval(), 'fx_synthetic_price': results[0][0],
              'fx_intervals_ms': {key: pet.config.fx[key] for key in
                                  ('interval_ms', 'okx_interval_ms', 'account_interval_ms')},
              'fx_short_warmup': True,
              'fx_jitter_anchor_age_seconds': round(129.99-jitter_engine.history[0][0], 3),
              'fx_jitter_percent': jitter_update.percent,
              'fx_synthetic_mood': update.mood,
              'sound_lengths_ms': sound_lengths, 'volume': pet.config.volume,
              'okx_public_fixture': True,
              'sensitivity_hot_update': True, 'sensitivity_fresh_confirmations': True,
              'instrument_catalog_fixture': True, 'ocr_instrument_selector_disabled': True,
              'okx_failure_limit': 3, 'okx_auto_recovery': True,
              'cross_source_history_cleared': True, 'ocr_day_baseline_reset': True,
              'fx_capture_flags': capture_flags, 'fx_debug_logging': True,
              'fx_test_defaults': {key: defaults[key] for key in ('exclude_from_capture', 'debug_log')},
              'external': str(external), 'bundled': str(bundled)}
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    pet.close()
    settings.clear()
    settings.sync()
    return 0
