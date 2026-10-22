"""Public instrument catalog and editable selector; no credential file access."""
import re
import time

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtWidgets import (QApplication, QComboBox, QCompleter, QFormLayout,
                              QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget)

from okx_api import DEFAULT_OKX, OKXClient

CACHE_SECONDS = 3600.


def normalize_instrument(text, inst_type):
    value = text.strip().upper()
    if inst_type not in ('SPOT', 'SWAP'):
        raise ValueError('请选择现货或永续')
    if value.endswith('-SWAP'):
        if inst_type != 'SWAP':
            raise ValueError('现货交易对不能带 -SWAP，请切换为永续或修改交易对')
        value = value[:-5]
    if not re.fullmatch(r'[A-Z0-9]+-[A-Z0-9]+', value) or len(value) > 59:
        raise ValueError('请输入完整交易对，例如 BTC-USDT；永续会自动补 -SWAP')
    return value + ('-SWAP' if inst_type == 'SWAP' else '')


def available_instruments(rows, inst_type):
    result = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('state') != 'live' or row.get('instType') != inst_type:
            continue
        value = row.get('instId')
        if not isinstance(value, str):
            continue
        try:
            if normalize_instrument(value, inst_type) == value:
                result.add(value)
        except ValueError:
            continue
    return sorted(result)


class CatalogSignals(QObject):
    done = Signal(str, bool, object)


class CatalogJob(QRunnable):
    def __init__(self, inst_type, simulated):
        super().__init__()
        self.inst_type, self.simulated = inst_type, simulated
        self.signals = CatalogSignals()

    def run(self):
        result = None
        try:
            client = OKXClient({**DEFAULT_OKX, 'simulated': self.simulated})
            result = available_instruments(client.instruments(self.inst_type), self.inst_type)
            if not result:
                result = None
        except Exception:
            pass  # Never pass server text or request details into UI/logs.
        self.signals.done.emit(self.inst_type, self.simulated, result)


class InstrumentCatalog(QObject):
    finished = Signal(str, bool, object, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cache, self.jobs = {}, {}
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)

    def request(self, inst_type, simulated=False, force=False):
        key = (inst_type, simulated)
        cached = self.cache.get(key)
        if cached:
            self.finished.emit(inst_type, simulated, list(cached[1]), False)
            if not force and time.monotonic() - cached[0] < CACHE_SECONDS:
                return
        if key in self.jobs:
            return
        job = CatalogJob(inst_type, simulated)
        job.signals.done.connect(self._received)
        self.jobs[key] = job
        self.pool.start(job)

    @Slot(str, bool, object)
    def _received(self, inst_type, simulated, result):
        key = (inst_type, simulated)
        self.jobs.pop(key, None)
        if result is not None:
            self.cache[key] = (time.monotonic(), result)
        cached = self.cache.get(key)
        self.finished.emit(inst_type, simulated, list(cached[1]) if cached else [], result is None)


_catalog = None


def shared_catalog():
    global _catalog
    if _catalog is None:
        # Outlive dialogs, so closing one while a request runs remains safe.
        _catalog = InstrumentCatalog(QApplication.instance())
    return _catalog


class InstrumentSelector(QWidget):
    def __init__(self, options, enabled, parent=None, catalog=None):
        super().__init__(parent)
        self.original = dict(options)
        self.okx_enabled = enabled
        self.simulated = options['simulated']
        self.catalog = catalog if catalog is not None else shared_catalog()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.controls = QWidget()
        form = QFormLayout(self.controls)
        form.setContentsMargins(0, 0, 0, 0)
        self.market = QComboBox()
        self.market.setObjectName('okx_inst_type')
        self.market.addItem('现货', 'SPOT')
        self.market.addItem('永续', 'SWAP')
        initial = 'SWAP' if options['inst_id'].endswith('-SWAP') else 'SPOT'
        self.market.setCurrentIndex(self.market.findData(initial))
        form.addRow('市场类型', self.market)
        self.instrument = QComboBox()
        self.instrument.setObjectName('okx_inst_id')
        self.instrument.setEditable(True)
        self.instrument.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.instrument.setMinimumContentsLength(15)
        self.instrument.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.instrument.setEditText(self.display_id(options['inst_id']))
        self.instrument.lineEdit().setPlaceholderText('搜索或手动输入，如 BTC-USDT')
        completer = self.instrument.completer()
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        row = QHBoxLayout()
        row.addWidget(self.instrument, 1)
        self.refresh = QPushButton('刷新')
        self.refresh.setObjectName('okx_instruments_refresh')
        row.addWidget(self.refresh)
        form.addRow('交易对', row)
        layout.addWidget(self.controls)
        self.actual = QLabel()
        self.actual.setWordWrap(True)
        self.actual.setObjectName('okx_actual_instrument')
        layout.addWidget(self.actual)
        self.status = QLabel('列表加载失败时可手动输入完整交易对。')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.market.currentIndexChanged.connect(self._market_changed)
        self.instrument.editTextChanged.connect(self._update_actual)
        self.refresh.clicked.connect(lambda: self.load(force=True))
        self.catalog.finished.connect(self._loaded)
        self.controls.setEnabled(enabled)
        self._update_actual()

    @staticmethod
    def display_id(value):
        return value[:-5] if value.endswith('-SWAP') else value

    def selection(self):
        # Unchanged old configurations remain valid even if not in a current list.
        text = self.instrument.currentText().strip().upper()
        original_type = 'SWAP' if self.original['inst_id'].endswith('-SWAP') else 'SPOT'
        if not self.okx_enabled or (self.market.currentData() == original_type
                                  and text == self.display_id(self.original['inst_id'])):
            return self.original['inst_id'], original_type
        value = normalize_instrument(text, self.market.currentData())
        return value, self.market.currentData()

    def _update_actual(self):
        if not self.okx_enabled:
            self.actual.setText('当前使用截图识别，照常读取框选区域；交易对选择仅在 OKX 数据源下启用。')
            return
        try:
            value, _ = self.selection()
            self.actual.setText('实际品种：' + value)
        except ValueError as error:
            self.actual.setText(str(error))

    def _market_changed(self):
        text = self.display_id(self.instrument.currentText().strip().upper())
        self.instrument.blockSignals(True)
        self.instrument.clear()
        self.instrument.setEditText(text)
        self.instrument.blockSignals(False)
        self._update_actual()
        self.load()

    def set_simulated(self, simulated):
        self.simulated = simulated
        self.load()

    def load(self, force=False):
        if not self.okx_enabled:
            return
        self.status.setText('正在加载列表，可先搜索或手动输入…')
        self.catalog.request(self.market.currentData(), self.simulated, force)

    @Slot(str, bool, object, bool)
    def _loaded(self, inst_type, simulated, values, failed):
        if not self.okx_enabled or inst_type != self.market.currentData() or simulated != self.simulated:
            return
        text = self.instrument.currentText()
        self.instrument.blockSignals(True)
        self.instrument.clear()
        self.instrument.addItems([self.display_id(value) for value in values])
        self.instrument.setCurrentIndex(-1)
        self.instrument.setEditText(text)
        self.instrument.blockSignals(False)
        self.status.setText(('列表加载失败，使用缓存；也可手动输入。' if values else '列表加载失败，可手动输入完整交易对。')
                            if failed else f'已载入 {len(values)} 个交易对；支持搜索和手动输入。')
        self._update_actual()
