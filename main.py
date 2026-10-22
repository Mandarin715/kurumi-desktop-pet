"""Kurumi Windows desktop pet. Run with pythonw main.py or build.ps1."""
import ctypes
import logging
import os
from pathlib import Path
import random
import sys
import json

from PySide6.QtCore import QEvent, QEasingCurve, QPoint, QRect, QRectF, QSettings, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import (QAction, QActionGroup, QColor, QFont, QIcon, QImage, QPainter,
                           QPainterPath, QPen, QPixmap, QPolygonF, QRegion, QTransform)
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
                              QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
                              QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget)

from pet_core import atomic_json, image_candidates, load_config, save_options
from fx_monitor import FXMonitor
from fx_settings import NoWheelDoubleSpinBox, SensitivityControls, VolumeControls
from fx_ocr import parse_price
from fx_region import RegionSelector, resolve_region
from pet_audio import PetAudio
from okx_api import DEFAULT_OKX, Credentials, load_credentials, validate_options
from okx_instruments import InstrumentSelector
from window_placement import place_dialog
from app_version import VERSION

APP_NAME = '久留美桌宠'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = 'KurumiDesktopPet'
BUNDLED = Path(__file__).resolve().parent
EXTERNAL = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else BUNDLED


def startup_command():
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    executable = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{executable}" "{Path(__file__).resolve()}"'


def startup_enabled():
    if sys.platform != 'win32':
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_NAME)
            return value == startup_command()
    except OSError:
        return False


def set_startup(enabled):
    if sys.platform != 'win32':
        raise OSError('开机自启仅支持 Windows')
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def alpha_region(pixmap):
    """Native input mask: transparent pixels never block desktop clicks."""
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    bits = image.constBits()
    region = QRegion()
    stride = image.bytesPerLine()
    for y in range(image.height()):
        start = None
        for x in range(image.width() + 1):
            solid = x < image.width() and bits[y * stride + x * 4 + 3] > 12
            if solid and start is None:
                start = x
            elif not solid and start is not None:
                region |= QRegion(start, y, x - start, 1)
                start = None
    return region


class PetWindow(QWidget):
    PET_SIZE = 300
    IMAGE_BOX = QRect(10, 114, 300, 300)

    def __init__(self, external=EXTERNAL, bundled=BUNDLED, settings=None, tray=True):
        super().__init__()
        self.external, self.bundled = Path(external), Path(bundled)
        self.config = load_config(external, bundled)
        self.settings = settings if settings is not None else QSettings('KurumiPet', 'DesktopPet')
        self.audio = PetAudio(external, bundled, self.settings, self.config.volume)
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.font = QFont('Microsoft YaHei UI', 10)
        self.setFont(self.font)
        self.images = {}
        for number in range(1, 15):
            for path in image_candidates(number, external, bundled):
                image = QPixmap(str(path))
                if not image.isNull():
                    image.setDevicePixelRatio(1.)
                    self.images[number] = image
                    break
            else:
                raise ValueError(f'无法读取第 {number} 张图片，请检查 assets/kurumi_{number:02d}.png。')
        self.current = self.config.default
        self.text = ''
        self.bubble_text = ''
        self.bubble_level = 0.
        self.bubble_rect = QRectF()
        self.transition = 1.
        self.previous_frame = None
        self.previous_region = QRegion()
        self.pose_animation = QVariantAnimation(self)
        self.pose_animation.setDuration(280)
        self.pose_animation.setStartValue(0.)
        self.pose_animation.setEndValue(1.)
        self.pose_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.pose_animation.valueChanged.connect(self.animate_pose)
        self.pose_animation.finished.connect(self.finish_pose_animation)
        self.bubble_animation = QVariantAnimation(self)
        self.bubble_animation.setDuration(190)
        self.bubble_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.bubble_animation.valueChanged.connect(self.animate_bubble)
        self.bubble_animation.finished.connect(self.finish_bubble_animation)
        try:
            saved_size = int(self.settings.value('size', 340))
        except (ValueError, TypeError):
            saved_size = 340
        self.set_pet_size(saved_size if saved_size in (260, 340, 420) else 340, save=False)
        self.press_global = None
        self.drag_origin = None
        self.dragging = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.reset_pose)
        self.menu = QMenu(self)
        self.fx = FXMonitor(self)
        self.fx_action = QAction('FX启动！', self, checkable=True)
        self.fx_action.triggered.connect(self.toggle_fx)
        self.menu.addAction(self.fx_action)
        sources = self.menu.addMenu('行情数据源')
        self.source_group = QActionGroup(self)
        self.source_actions = {}
        for source, label in [('ocr', '截图识别'), ('okx', '欧易 OKX API')]:
            action = QAction(label, self, checkable=True)
            action.setData(source)
            action.setChecked(self.config.data_source == source)
            self.source_group.addAction(action)
            self.source_actions[source] = action
            sources.addAction(action)
        self.source_group.triggered.connect(lambda action: self.select_source(action.data()))
        self.fx_status_action = self.menu.addAction('FX：已关闭')
        self.fx_status_action.setEnabled(False)
        self.fx.status.connect(self.update_fx_status)
        self.fx.reading.connect(self.react_to_price)
        self.fx.source_changed.connect(lambda source: self.source_actions[source].setChecked(True))
        self.fx.notice.connect(self.show_fx_notice)
        self.fx.needs_region.connect(self.select_fallback_region)
        self.fx.permission_warning.connect(self.show_permission_warning)
        self.menu.addAction('FX设置…', lambda: self.fx_settings())
        self.selector = None
        self.menu.addSeparator()
        self.mute_action = QAction('静音', self, checkable=True)
        self.mute_action.setChecked(self.audio.muted)
        self.mute_action.triggered.connect(self.audio.set_muted)
        self.menu.addAction(self.mute_action)
        sizes = self.menu.addMenu('桌宠大小')
        self.size_group = QActionGroup(self)
        for label, size in [('小 · 260', 260), ('标准 · 340', 340), ('大 · 420', 420)]:
            action = QAction(label, self, checkable=True)
            action.setData(size)
            action.setChecked(self.PET_SIZE == size)
            self.size_group.addAction(action)
            sizes.addAction(action)
        self.size_group.triggered.connect(lambda action: self.set_pet_size(action.data()))
        self.startup_action = QAction('开机自启', self, checkable=True)
        self.startup_action.triggered.connect(self.toggle_startup)
        self.menu.addAction(self.startup_action)
        self.menu.addAction('退出', QApplication.instance().quit)
        self.menu.aboutToShow.connect(lambda: self.startup_action.setChecked(startup_enabled()))
        self.icon = QIcon(self.images[self.config.default])
        self.setWindowIcon(self.icon)
        self.tray = None
        if tray and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.icon, self)
            self.tray.setToolTip(APP_NAME + ' · 左键互动 / 拖动移动 / 右键菜单')
            self.tray.setContextMenu(self.menu)
            self.tray.activated.connect(self.tray_activated)
            self.tray.show()
        self.restore_position()
        self.update_mask()
        QApplication.instance().aboutToQuit.connect(self.save_position)
        QApplication.instance().aboutToQuit.connect(lambda: self.fx.set_active(False))
        QApplication.instance().aboutToQuit.connect(self.audio.close)
        for screen in QApplication.screens():
            screen.availableGeometryChanged.connect(self.keep_on_screen)
        QApplication.instance().screenRemoved.connect(self.keep_on_screen)

    def set_pet_size(self, size, save=True):
        self.pose_animation.stop()
        self.previous_frame = None
        self.transition = 1.
        self.PET_SIZE = size
        self.IMAGE_BOX = QRect(10, 114, size, size)
        self.setFixedSize(size + 20, size + 124)
        self.regions, self.image_rects = {}, {}
        for number, original in self.images.items():
            mask = original.scaled(self.IMAGE_BOX.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            rect = QRect(0, 0, mask.width(), mask.height())
            rect.moveCenter(self.IMAGE_BOX.center())
            self.image_rects[number] = rect
            self.regions[number] = alpha_region(mask).translated(rect.topLeft())
        self.refresh_density()
        if self.bubble_text:
            self.layout_bubble()
        self.update_mask()
        if save:
            self.settings.setValue('size', size)
            self.keep_on_screen()
            self.save_position()
        self.update()

    def refresh_density(self):
        # Cache from ORIGINAL 1024px images at the actual screen's pixel density.
        ratio = max(1., self.devicePixelRatioF())
        self.render_images = {}
        for number, original in self.images.items():
            rect = self.image_rects[number]
            scaled = original.scaled(round(rect.width()*ratio), round(rect.height()*ratio),
                                     Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(ratio)
            self.render_images[number] = scaled
        self.update()

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.Type.DevicePixelRatioChange and hasattr(self, 'render_images'):
            QTimer.singleShot(0, self.refresh_density)
        return result

    def toggle_fx(self, enabled):
        if enabled:
            if self.config.data_source == 'ocr':
                self.select_price_region(start_after=True)
                return
            self.timer.stop()
            self.show_dialogue('')
            self.fx.set_active(True)
        else:
            summary = self.fx.set_active(False)
            self.start_pose(self.config.default)
            self.show_dialogue(summary)
            self.timer.start(max(8000, self.config.duration))

    def select_source(self, source):
        try:
            save_options(self.config, self.external, self.bundled, source, self.config.okx)
        except ValueError as error:
            self.source_actions[self.fx.source].setChecked(True)
            self.show_warning(str(error))
            return
        if self.fx.active:
            if source == 'ocr':
                self.select_price_region(start_after=True)
            else:
                self.fx.set_active(True, preserve_history=True)
        else:
            self.fx.source = source

    def show_fx_notice(self, text):
        if self.fx.active:
            self.show_dialogue(text)
            self.timer.start(max(4000, self.config.duration))

    def show_permission_warning(self, text):
        self.permission_box = QMessageBox(QMessageBox.Icon.Warning, APP_NAME, text,
                                         QMessageBox.StandardButton.Ok, self)
        self.permission_box.adjustSize()
        place_dialog(self.permission_box, self)
        self.permission_box.open()

    def show_warning(self, text, parent=None):
        box = QMessageBox(QMessageBox.Icon.Warning, APP_NAME, text,
                          QMessageBox.StandardButton.Ok, parent or self)
        box.adjustSize()
        place_dialog(box, self)
        box.exec()

    def select_fallback_region(self):
        if self.fx.active and self.fx.source == 'ocr' and resolve_region(self.fx.region) is None:
            self.select_price_region(start_after=True, source_override='ocr')

    def select_price_region(self, start_after=False, source_override=None):
        if self.selector is not None:
            return
        was_active = self.fx.active
        if was_active:
            self.fx.set_active(False)
        self.timer.stop()
        self.show_dialogue('')

        def selected(data):
            self.selector = None
            if data is not None:
                self.fx.set_region(data)
            # An explicit OCR start must never fall back to yesterday's region
            # when the user cancels the fresh selection.
            resume = start_after and data is not None
            self.fx_action.setChecked(resume)
            if resume:
                self.fx.set_active(True, source_override=source_override,
                                   preserve_history=False)
        self.selector = RegionSelector(selected)

    def fx_settings(self, load_instruments=True):
        dialog = QDialog(self)
        dialog.setWindowTitle('FX设置 · 截图 / OKX')
        dialog.resize(570, min(680, QApplication.primaryScreen().availableGeometry().height()-80))
        outer = QVBoxLayout(dialog)
        scroll = QScrollArea(dialog)
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        info = QLabel('右键“行情数据源”选择截图或 OKX，选择保存在 kurumi.json。\n'
                      f'截图模式每次开启 FX 都先框选一个价格数字，之后每 {self.config.fx["interval_ms"]/1000:g} 秒读取该区域。\n'
                      f'OKX 行情每 {self.config.fx["okx_interval_ms"]/1000:g} 秒读取，账户独立每 {self.config.fx["account_interval_ms"]/1000:g} 秒读取。\n'
                      '行情窗口移动、切换品种或缩放后，请关闭再开启 FX 重新框选。\n'
                      '读不清、包含多个数字或两次 OCR 结果不同，均跳过本次。')
        info.setWordWrap(True)
        layout.addWidget(info)
        region_label = '截图模式每次开启 FX 时都会重新框选价格区域'
        status_label = QLabel(region_label + '\n状态：' + self.fx.status_text)
        status_label.setWordWrap(True)
        layout.addWidget(status_label)
        sensitivity = SensitivityControls(self.config.fx)
        layout.addWidget(sensitivity)
        original_volume = self.audio.volume
        volume = VolumeControls(original_volume, self.audio.muted)
        volume.changed.connect(self.audio.set_volume)
        layout.addWidget(volume)
        form = QFormLayout()
        stop = QLineEdit('' if self.fx.stop_loss() is None else f'{self.fx.stop_loss():.12f}'.rstrip('0').rstrip('.'))
        stop.setObjectName('fx_stop_loss')
        stop.setPlaceholderText('留空关闭止损提醒，例如 98.50')
        form.addRow('止损价', stop)
        layout.addLayout(form)
        layout.addWidget(QLabel('OKX 公共行情（无需密钥）'))
        okx_form = QFormLayout()
        instrument = InstrumentSelector(self.config.okx, self.config.data_source == 'okx')
        layout.addWidget(instrument)
        simulated = QCheckBox('模拟盘（使用对应模拟盘密钥）')
        simulated.setChecked(self.config.okx['simulated'])
        okx_form.addRow(simulated)
        simulated.toggled.connect(instrument.set_simulated)
        position_loss, day_loss = NoWheelDoubleSpinBox(), NoWheelDoubleSpinBox()
        for spin, key in [(position_loss, 'position_stop_loss_percent'), (day_loss, 'daily_loss_percent')]:
            spin.setObjectName('okx_'+key)
            spin.setRange(.01, 1000.)
            spin.setDecimals(2)
            spin.setSuffix(' %')
            spin.setValue(self.config.okx[key])
        okx_form.addRow('持仓浮亏提醒线', position_loss)
        okx_form.addRow('今日观测权益亏损线', day_loss)
        layout.addLayout(okx_form)
        explanation = QLabel('账户读取覆盖全部持仓：任一有效持仓浮亏率超线会提醒止损。\n'
                             '今日观测权益按 UTC+8 当天首次读到的账户总权益比较，转入转出也会影响结果；\n'
                             '它不是欧易官方当日盈亏。账户止损开启后，无需手填上面的止损价。')
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        credential_error = ''
        try:
            existing = load_credentials(self.external)
        except ValueError:
            existing = Credentials()
            credential_error = '本地凭据文件无效，请重新填写；'
        layout.addWidget(QLabel(credential_error+'账户读取可选：三项全部留空只用公共行情。'))
        secret_form = QFormLayout()
        credential_fields = []
        for key, label in [('api_key', 'API Key'), ('secret_key', 'Secret Key'), ('passphrase', 'Passphrase')]:
            edit = QLineEdit(getattr(existing, key))
            edit.setObjectName('okx_'+key)
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            secret_form.addRow(label, edit)
            credential_fields.append(edit)
        layout.addLayout(secret_form)
        privacy = QLabel('隐私：截图只在本地内存中处理，不保存、不上传。\n'
                         '使用随程序附带的 RapidOCR 本地模型，无需联网。\n'
                         'OKX 模式仅向欧易读取数据，绝无下单、撤单、转账或提币功能。\n'
                         '密钥只存 exe 旁 okx.local.json，不进日志、不传任何模型、不打包。\n'
                         '本地另存区域、止损、偏好、当天首尾价格和观测权益基准。\n'
                         '表情灵敏度保存后立即生效，不清空价格窗口；切换品种会重新预热。\n'
                         '保持时间和两档台词仍可在 kurumi.json 的 fx 中修改，重启生效。')
        privacy.setWordWrap(True)
        layout.addWidget(privacy)
        restore_values = QPushButton('恢复数值默认')
        restore_values.setObjectName('fx_restore_numeric_defaults')
        restore_values.setToolTip('恢复默认灵敏度、30% 音量、两条 3% 账户提醒线，并清空手填止损价；保存后保留。交易对和密钥不变。')

        def reset_numeric_values():
            sensitivity.restore_default()
            volume.restore_default()
            position_loss.setValue(DEFAULT_OKX['position_stop_loss_percent'])
            day_loss.setValue(DEFAULT_OKX['daily_loss_percent'])
            stop.clear()

        restore_values.clicked.connect(reset_numeric_values)
        layout.addWidget(restore_values)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        outer.addWidget(buttons)
        def set_valid(valid):
            buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(valid)
        sensitivity.validity_changed.connect(set_valid)
        set_valid(sensitivity.valid)
        def save():
            value = parse_price(stop.text()) if stop.text().strip() else None
            if stop.text().strip() and value is None:
                self.show_warning('请输入一个大于 0 的价格数字，或留空关闭。', dialog)
                return
            try:
                policy = sensitivity.policy()
                inst_id, inst_type = instrument.selection()
                options = validate_options({**self.config.okx, 'inst_id': inst_id, 'inst_type': inst_type,
                                            'simulated': simulated.isChecked(),
                                            'position_stop_loss_percent': position_loss.value(),
                                            'daily_loss_percent': day_loss.value()})
                credentials = Credentials(*(edit.text().strip() for edit in credential_fields))
                if credentials.present and not credentials.complete:
                    raise ValueError('账户读取需要完整填写 API Key、Secret Key 和 Passphrase；或三项全部留空。')
                if any('\n' in edit.text() or '\r' in edit.text() or len(edit.text()) > 1024 for edit in credential_fields):
                    raise ValueError('密钥格式无效')
                changed = options != self.config.okx or credentials != existing
                same_instrument = all(options[key] == self.config.okx[key] for key in ('inst_id', 'simulated'))
                sensitivity_changed = sensitivity.dirty
                save_options(self.config, self.external, self.bundled, self.config.data_source, options,
                             fx=policy if sensitivity.dirty else None,
                             volume=volume.slider.value()/100 if volume.dirty else None)
                credential_path = self.external / 'okx.local.json'
                if credentials != existing:
                    atomic_json(credential_path, {key: getattr(credentials, key) for key in ('api_key', 'secret_key', 'passphrase')})
            except ValueError as error:
                self.show_warning('设置未保存：' + str(error), dialog)
                return
            except OSError:
                self.show_warning('设置未保存：请检查本地文件的写入权限。', dialog)
                return
            if not same_instrument and self.config.data_source == 'okx':
                value = None
            self.settings.setValue('fx/stop_loss', str(value) if value is not None else '')
            self.settings.sync()
            if self.fx.engine and self.fx.engine.stop_loss != value:
                self.fx.engine.stop_loss = float(value) if value is not None else None
                self.fx.engine.below_stop = False
            # Reset a changed instrument before evaluating thresholds: returns
            # from the previous symbol must not produce a new pose on Save.
            self.fx.policy = self.config.fx
            if changed:
                self.fx.reconfigure_okx()
            if sensitivity_changed:
                self.fx.apply_policy(self.config.fx)
            dialog.accept()
            if not same_instrument and self.config.data_source == 'okx':
                QTimer.singleShot(0, lambda: self.show_warning('已切换交易品种或模拟盘状态，手填止损价已清空，请按新品种重新设置。'))
        buttons.accepted.connect(lambda: save())
        buttons.rejected.connect(dialog.reject)
        if load_instruments:
            QTimer.singleShot(0, instrument.load)
        place_dialog(dialog, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.audio.set_volume(original_volume)
        dialog.deleteLater()

    def update_fx_status(self, status):
        self.fx_status_action.setText('FX：' + status)
        if self.tray:
            self.tray.setToolTip(APP_NAME + '\nFX：' + status)

    def react_to_price(self, update):
        before = self.current
        if update.pose is not None:
            self.start_pose(update.pose)
        report_audio = self.fx.pose_audio_reporter(before, self.current, update)
        if report_audio:
            self.audio.fx_pose_changed(before, self.current, on_result=report_audio)
        else:
            self.audio.fx_pose_changed(before, self.current)
        if update.pose is not None or update.text is not None:
            lines = (self.config.fx['lines_intense'][str(self.current)] if update.tier
                     else self.config.lines[self.current])
            self.show_dialogue(update.text or random.choice(lines))
            self.timer.start(self.config.duration)

    def restore_position(self):
        position = self.settings.value('position')
        if isinstance(position, QPoint):
            self.move(position)
        else:
            area = QApplication.primaryScreen().availableGeometry()
            self.move(area.right() - self.width() - 32, area.bottom() - self.height() - 20)
        self.keep_on_screen()

    def keep_on_screen(self, *_):
        screens = QApplication.screens()
        if not screens:
            return
        center = self.geometry().center()
        screen = QApplication.screenAt(center) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        x = max(area.left(), min(self.x(), area.right() - self.width() + 1))
        y = max(area.top(), min(self.y(), area.bottom() - self.height() + 1))
        self.move(x, y)

    def save_position(self):
        self.settings.setValue('position', self.pos())
        self.settings.sync()

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.keep_on_screen()
            self.show()
            self.raise_()

    def toggle_startup(self, enabled):
        try:
            set_startup(enabled)
        except OSError as error:
            self.startup_action.setChecked(startup_enabled())
            self.show_warning(f'开机自启设置失败：{error}')

    def react(self):
        if self.fx.active:
            self.fx.manual()
            return
        self.display_pose(random.choice([number for number in self.images if number != self.current]))
        self.audio.play('click')

    def display_pose(self, number):
        self.start_pose(number)
        self.show_dialogue(random.choice(self.config.lines[number]))
        self.timer.start(self.config.duration)

    def start_pose(self, number):
        if number == self.current:
            return
        # Snapshot the current blended frame so fast repeated clicks never snap back.
        ratio = max(1., self.devicePixelRatioF())
        snapshot = QPixmap(round(self.width()*ratio), round(self.height()*ratio))
        snapshot.setDevicePixelRatio(ratio)
        snapshot.fill(Qt.GlobalColor.transparent)
        painter = QPainter(snapshot)
        self.paint_character(painter)
        painter.end()
        # The outgoing frame remains visible during the fade on Windows too.
        # Keep its pixel coverage for OCR occlusion checks, without installing
        # a binary native window mask that would make soft edges jagged.
        previous_region = self.character_region()
        self.pose_animation.stop()
        self.previous_frame = snapshot
        self.previous_region = previous_region
        self.current = number
        self.transition = 0.
        self.pose_animation.start()
        self.update_mask()
        self.update()

    def animate_pose(self, value):
        self.transition = float(value)
        self.update_mask()
        self.update()

    def finish_pose_animation(self):
        self.previous_frame = None
        self.previous_region = QRegion()
        self.transition = 1.
        self.update_mask()
        self.update()

    def show_dialogue(self, text):
        self.text = text
        self.bubble_animation.stop()
        if text:
            self.bubble_text = text
            self.layout_bubble()
        self.bubble_animation.setStartValue(self.bubble_level)
        self.bubble_animation.setEndValue(1. if text else 0.)
        self.bubble_animation.start()
        self.update_mask()
        self.update()

    def layout_bubble(self):
        # Fit any replacement dialogue without allowing it to cover the character.
        from PySide6.QtGui import QFontMetrics
        metrics = QFontMetrics(self.font)
        flags = Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere
        max_width = self.width() - 24
        bounds = metrics.boundingRect(QRect(0, 0, max_width-32, 1000), flags, self.bubble_text)
        width = min(max_width, max(100, bounds.width() + 32))
        height = min(92, max(44, bounds.height() + 24))
        self.bubble_rect = QRectF((self.width() - width) / 2, 104 - height, width, height)

    def animate_bubble(self, value):
        self.bubble_level = float(value)
        self.update_mask()
        self.update()

    def finish_bubble_animation(self):
        if not self.text:
            self.bubble_text = ''
        self.update_mask()

    def reset_pose(self):
        self.timer.stop()
        if not self.fx.active:
            self.start_pose(self.config.default)
        self.show_dialogue('')

    def bubble_path(self):
        path = QPainterPath()
        path.addRoundedRect(self.bubble_rect, 14, 14)
        center = self.width() // 2
        tail = QPolygonF([QPoint(center-10, 103), QPoint(center, 113), QPoint(center+10, 103)])
        path.addPolygon(tail)
        return path.simplified()

    def character_region(self):
        region = QRegion()
        if self.transition > .001:
            offset = round(6 * (1-self.transition))
            region = self.regions[self.current].translated(0, offset)
        if self.previous_frame is not None and self.transition < .999:
            region |= self.previous_region
        return region

    def capture_region(self):
        """Visible painted coverage in logical widget coordinates, excluding empty padding."""
        region = self.character_region()
        if self.bubble_text and self.bubble_level > .001:
            anchor = self.bubble_rect.center()
            scale = .92 + .08*self.bubble_level
            transform = QTransform()
            transform.translate(anchor.x(), anchor.y())
            transform.scale(scale, scale)
            transform.translate(-anchor.x(), -anchor.y())
            region |= QRegion(transform.map(self.bubble_path()).toFillPolygon().toPolygon())
        return region.intersected(QRegion(self.rect()))

    def occludes_price_rect(self, global_rect):
        if not self.isVisible():
            return False
        # QScreen geometry and selection use logical desktop coordinates.
        # mapFromGlobal handles negative monitor origins and window placement;
        # multiplying either rectangle by DPR here would mix coordinate spaces.
        local_rect = QRect(self.mapFromGlobal(global_rect.topLeft()), global_rect.size())
        return self.capture_region().intersects(local_rect)

    def update_mask(self):
        if QApplication.platformName() == 'windows':
            # A Windows layered window already passes clicks through alpha=0.
            # A binary region mask would clip soft edges to whole logical pixels
            # and produce visibly jagged outlines at 150%/200% DPI.
            self.clearMask()
            return
        region = self.character_region()
        if self.bubble_text and self.bubble_level > .01:
            region |= QRegion(self.bubble_path().toFillPolygon().toPolygon())
        self.setMask(region)

    def paint_character(self, painter):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self.previous_frame is not None and self.transition < 1.:
            painter.setOpacity(1-self.transition)
            painter.drawPixmap(0, 0, self.previous_frame)
        painter.setOpacity(self.transition)
        rect = self.image_rects[self.current].translated(0, round(6 * (1-self.transition)))
        painter.drawPixmap(rect, self.render_images[self.current])
        painter.restore()

    def paintEvent(self, event):
        painter = QPainter(self)
        self.paint_character(painter)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.bubble_text and self.bubble_level > .001:
            painter.save()
            painter.setOpacity(self.bubble_level)
            anchor = self.bubble_rect.center()
            scale = .92 + .08*self.bubble_level
            painter.translate(anchor)
            painter.scale(scale, scale)
            painter.translate(-anchor)
            painter.setPen(QPen(QColor('#d989ab'), 1.4))
            painter.setBrush(QColor('#fff9fc'))
            painter.drawPath(self.bubble_path())
            painter.setPen(QColor('#583547'))
            painter.setFont(self.font)
            painter.drawText(self.bubble_rect.adjusted(16, 10, -16, -10),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap |
                             Qt.TextFlag.TextWrapAnywhere, self.bubble_text)
            painter.restore()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.press_global = event.globalPosition().toPoint()
            self.drag_origin = self.pos()
            self.dragging = False
            event.accept()

    def mouseMoveEvent(self, event):
        if self.press_global is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self.press_global
            if delta.manhattanLength() >= QApplication.startDragDistance():
                self.dragging = True
            if self.dragging:
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.move(self.drag_origin + delta)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.press_global is not None:
            if self.dragging:
                self.keep_on_screen()
                self.save_position()
            else:
                self.react()
            self.press_global = None
            self.drag_origin = None
            self.dragging = False
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            event.accept()

    def mouseDoubleClickEvent(self, event):
        self.mousePressEvent(event)

    def contextMenuEvent(self, event):
        self.menu.exec(event.globalPos())

    def closeEvent(self, event):
        self.audio.close()
        self.fx.set_active(False)
        self.save_position()
        if self.tray:
            self.tray.hide()
        event.accept()


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--apply-update':
        from updater import run_installer
        return run_installer(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
        # Use the native Windows font engine for Chinese glyph verification.
        os.environ['QT_QPA_PLATFORM'] = 'windows' if sys.platform == 'win32' else 'offscreen'
        app = QApplication(sys.argv)
        from smoke_check import run
        return run(sys.argv[2], EXTERNAL, BUNDLED)
    # A named mutex also protects against two one-file launchers extracting at once.
    mutex = None
    if sys.platform == 'win32':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        mutex = kernel.CreateMutexW(None, False, r'Local\KurumiDesktopPet-v1')
        if ctypes.get_last_error() == 183:
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(mutex)
            return 0
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    app.setQuitOnLastWindowClosed(False)
    log_dir = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'KurumiPet'
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_dir / 'kurumi.log', level=logging.WARNING,
                        format='%(asctime)s %(levelname)s %(message)s', encoding='utf-8')
    try:
        pet = PetWindow()
    except Exception as error:
        logging.exception('桌宠启动失败')
        QMessageBox.critical(None, APP_NAME, f'桌宠启动失败：\n{error}')
        return 1
    pet.show()
    if len(sys.argv) == 3 and sys.argv[1] == '--update-receipt':
        from updater import write_start_receipt
        QTimer.singleShot(0, lambda: write_start_receipt(sys.argv[2]))
    # Source runs and --self-test never check the network or offer installs.
    if getattr(sys, 'frozen', False):
        from update_ui import UpdateController
        pet.updates = UpdateController(pet)
        QTimer.singleShot(3000, pet.updates.check)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
