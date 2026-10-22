"""Small pink update dialog. All network and file preparation run off the GUI thread."""
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QProgressBar,
                              QPushButton, QTextBrowser, QVBoxLayout)

from app_version import VERSION
from updater import (BAIDU_URL, QUARK_URL, Cancelled, UpdateError, check_latest,
                     cleanup_finished_sessions, download_release, launch_program,
                     prepare_install, slow_download, stage_archive)
from window_placement import place_dialog


class Worker(QObject):
    checked = Signal(object)
    progress = Signal(int, int)
    prepared = Signal(object)
    failed = Signal(str)

    def __init__(self, operation, *args):
        super().__init__()
        self.operation, self.args = operation, args
        self.cancel = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name='KurumiUpdate')
        self.thread.start()

    def run(self):
        if self.operation == 'check':
            cleanup_finished_sessions()
            self.checked.emit(check_latest(skipped=self.args[0]))
            return
        work = Path(tempfile.mkdtemp(prefix='KurumiUpdate-'))
        try:
            release, target = self.args
            archive = download_release(release, work/'download.zip', self.cancel, self.progress.emit)
            files = stage_archive(archive, work/'stage', release.version)
            if self.cancel.is_set():
                raise Cancelled()
            plan, helper = prepare_install(work, target, files)
            if self.cancel.is_set():
                raise Cancelled()
            self.prepared.emit((work, plan, helper))
        except Cancelled:
            shutil.rmtree(work, ignore_errors=True)
            self.failed.emit('')
        except Exception as error:
            shutil.rmtree(work, ignore_errors=True)
            self.failed.emit(str(error) if isinstance(error, UpdateError)
                             else '无法准备更新，请使用网盘下载。')


class UpdateDialog(QDialog):
    download_requested = Signal()
    skip_requested = Signal(str)
    cancel_requested = Signal()

    def __init__(self, release, pet, current=VERSION):
        super().__init__(pet)
        self.release = release
        self.busy = False
        self.installing = False
        self.setObjectName('kurumi_update')
        self.setWindowTitle('久留美有新版本啦')
        self.setWindowIcon(pet.windowIcon())
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setFont(QFont('Microsoft YaHei UI', 9))
        self.resize(480, 378)
        self.setMinimumSize(380, 300)
        self.setStyleSheet('''
            QDialog {background:#fff6fa; color:#61394c;}
            QLabel {color:#61394c; background:transparent;}
            QLabel#update_title {font-size:19px; font-weight:700; color:#a73f70;}
            QLabel#update_subtitle {color:#95677d;}
            QTextBrowser {background:#ffffff; color:#61394c; border:1px solid #efccdc;
                border-radius:10px; padding:9px; selection-background-color:#f4b7d1;}
            QPushButton {background:#fff; color:#975171; border:1px solid #e9bbcf;
                border-radius:7px; padding:7px 12px; min-height:17px;}
            QPushButton:hover {background:#fce4ef; border-color:#d47b9f;}
            QPushButton:disabled {color:#ba9baa; background:#f5edf1;}
            QPushButton#update_now {background:#d96198; color:white; border-color:#d96198; font-weight:600;}
            QPushButton#update_now:hover {background:#c74e86;}
            QProgressBar {border:1px solid #ecc5d7; border-radius:5px; background:white;
                color:#8e4567; text-align:center; min-height:15px;}
            QProgressBar::chunk {background:#eda3c3; border-radius:4px;}
        ''')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(9)
        header = QHBoxLayout()
        icon = QLabel()
        picture = pet.images.get(10) if hasattr(pet, 'images') else None
        if picture:
            icon.setPixmap(picture.scaled(55, 55, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
        header.addWidget(icon)
        titles = QVBoxLayout()
        title = QLabel('久留美有新版本啦！')
        title.setObjectName('update_title')
        titles.addWidget(title)
        subtitle = QLabel(f'当前 v{current}  →  新版 v{release.version}')
        subtitle.setObjectName('update_subtitle')
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        layout.addLayout(header)
        self.notes = QTextBrowser()
        self.notes.setObjectName('update_release_notes')
        # Treat remote notes as text, not HTML with remote images/resources.
        self.notes.setPlainText(release.notes)
        self.notes.setOpenExternalLinks(False)
        layout.addWidget(self.notes, 1)
        self.status = QLabel('自动更新会保留配置、密钥和自定义素材。')
        self.status.setWordWrap(True)
        self.status.setObjectName('update_status')
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()
        layout.addWidget(self.progress)
        links = QHBoxLayout()
        links.setSpacing(8)
        self.baidu = QPushButton('百度网盘')
        self.baidu.setObjectName('update_baidu')
        self.quark = QPushButton('夸克网盘')
        self.quark.setObjectName('update_quark')
        for button, url in ((self.baidu, BAIDU_URL), (self.quark, QUARK_URL)):
            button.clicked.connect(lambda _=False, value=url: self.open_mirror(value))
            links.addWidget(button)
        mirror_tip = QLabel('网盘下载请核对文件版本')
        mirror_tip.setObjectName('update_subtitle')
        links.addWidget(mirror_tip, 1)
        layout.addLayout(links)
        buttons = QHBoxLayout()
        self.skip = QPushButton('跳过此版本')
        self.skip.setObjectName('update_skip')
        self.skip.clicked.connect(self.skip_version)
        buttons.addWidget(self.skip)
        buttons.addStretch()
        self.later = QPushButton('稍后')
        self.later.setObjectName('update_later')
        self.later.clicked.connect(self.reject)
        buttons.addWidget(self.later)
        self.update_button = QPushButton('更新')
        self.update_button.setObjectName('update_now')
        self.update_button.clicked.connect(self.download_requested)
        buttons.addWidget(self.update_button)
        layout.addLayout(buttons)

    def open_mirror(self, url):
        QDesktopServices.openUrl(QUrl(url))
        self.status.setText('已打开网盘，请核对版本；手动替换前请退出桌宠，保留个人配置和自定义素材。')

    def skip_version(self):
        self.skip_requested.emit(self.release.version)
        self.reject()

    def set_downloading(self):
        self.busy = True
        self.progress.show()
        self.progress.setValue(0)
        self.update_button.setEnabled(False)
        self.skip.setEnabled(False)
        self.later.setText('取消下载')
        self.status.setText('正在从 GitHub 下载…下载后将校验并重启。')

    def failed(self, message):
        self.busy = False
        self.installing = False
        self.update_button.setEnabled(True)
        self.update_button.setText('重试更新')
        self.skip.setEnabled(True)
        self.later.setText('稍后')
        self.status.setText(message)

    def reject(self):
        if self.installing:
            return  # The helper has been handed off; prevent a misleading cancel.
        if self.busy:
            self.cancel_requested.emit()
        super().reject()


class UpdateController(QObject):
    def __init__(self, pet, worker_factory=Worker):
        super().__init__(pet)
        self.pet, self.settings, self.worker_factory = pet, pet.settings, worker_factory
        self.check_worker = self.download_worker = None
        self.dialog = None
        self.work = self.helper = None
        self.started = self.last_data = 0.
        self.received = 0
        self.watch = QTimer(self)
        self.watch.setInterval(500)
        self.watch.timeout.connect(self.check_progress)
        QApplication.instance().aboutToQuit.connect(self.cancel)

    def check(self):
        if self.check_worker is not None:
            return
        self.check_worker = self.worker_factory('check', str(self.settings.value('updates/skipped', '')))
        self.check_worker.checked.connect(self.offer)
        self.check_worker.start()

    @Slot(object)
    def offer(self, release):
        self.check_worker = None
        if release is None or (self.dialog is not None and self.dialog.isVisible()):
            return
        # Do not interrupt a modal settings window or active price selection.
        if QApplication.activeModalWidget() or getattr(self.pet, 'selector', None):
            QTimer.singleShot(1500, lambda: self.offer(release))
            return
        self.dialog = UpdateDialog(release, self.pet)
        self.dialog.download_requested.connect(self.download)
        self.dialog.skip_requested.connect(self.skip)
        self.dialog.cancel_requested.connect(self.cancel)
        place_dialog(self.dialog, self.pet)
        self.dialog.show()

    def skip(self, version):
        self.settings.setValue('updates/skipped', version)
        self.settings.sync()

    def download(self):
        if self.download_worker:
            return
        if not getattr(sys, 'frozen', False):
            self.dialog.failed('源码运行时请使用网盘下载 exe 版本。')
            return
        self.dialog.set_downloading()
        self.started = self.last_data = time.monotonic()
        self.received = 0
        self.download_worker = self.worker_factory('download', self.dialog.release, Path(sys.executable))
        self.download_worker.progress.connect(self.on_progress)
        self.download_worker.prepared.connect(self.on_prepared)
        self.download_worker.failed.connect(self.on_failed)
        self.download_worker.start()
        self.watch.start()

    @Slot(int, int)
    def on_progress(self, received, total):
        if not self.dialog or not self.dialog.busy:
            return
        self.received, self.last_data = received, time.monotonic()
        self.dialog.progress.setValue(round(received/total*100))
        if received == total:
            self.dialog.status.setText('下载完成，正在校验并准备更新…')

    def check_progress(self):
        if not self.download_worker or not self.dialog or not self.dialog.busy:
            return
        now = time.monotonic()
        if self.received < self.dialog.release.size and slow_download(now-self.started, now-self.last_data, self.received):
            self.dialog.status.setText('GitHub 下载较慢，可使用百度网盘或夸克网盘；也可以继续等待或取消下载。')

    def cancel(self):
        if self.download_worker:
            self.download_worker.cancel.set()
        self.watch.stop()

    @Slot(str)
    def on_failed(self, message):
        self.watch.stop()
        self.download_worker = None
        if message and self.dialog and self.dialog.isVisible():
            self.dialog.failed(message)

    @Slot(object)
    def on_prepared(self, result):
        self.watch.stop()
        work, plan, helper = result
        if (not self.download_worker or self.download_worker.cancel.is_set()
                or not self.dialog or not self.dialog.isVisible()):
            shutil.rmtree(work, ignore_errors=True)
            self.download_worker = None
            return
        self.download_worker = None
        self.work = work
        try:
            self.helper = launch_program(helper, ['--apply-update', plan])
            self.helper_started = time.monotonic()
            self.dialog.installing = True
            self.dialog.later.setEnabled(False)
            self.dialog.baidu.setEnabled(False)
            self.dialog.quark.setEnabled(False)
            self.dialog.status.setText('校验通过，准备重启久留美…')
            QTimer.singleShot(100, self.wait_helper)
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            self.dialog.later.setEnabled(True)
            self.dialog.baidu.setEnabled(True)
            self.dialog.quark.setEnabled(True)
            self.dialog.failed('无法启动更新程序，请使用网盘下载。')

    def wait_helper(self):
        if (self.work/'helper-ready').is_file() and self.helper.poll() is None:
            self.pet.settings.sync()
            QApplication.instance().quit()
        elif self.helper.poll() is not None or time.monotonic()-self.helper_started > 15:
            if self.helper.poll() is None:
                import subprocess
                subprocess.run(['taskkill', '/PID', str(self.helper.pid), '/T', '/F'],
                               creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, timeout=10)
            self.dialog.later.setEnabled(True)
            self.dialog.baidu.setEnabled(True)
            self.dialog.quark.setEnabled(True)
            self.dialog.failed('更新程序未能启动，请使用网盘下载。')
        else:
            QTimer.singleShot(100, self.wait_helper)
