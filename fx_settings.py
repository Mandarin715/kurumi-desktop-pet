"""Sensitivity editor. No persistence or market access until the user saves."""
from copy import deepcopy

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                              QLabel, QPushButton, QVBoxLayout, QWidget)

from fx_logic import SENSITIVITY_PRESETS, sensitivity_name, sensitivity_values, validated_policy


class PercentSpinBox(QDoubleSpinBox):
    def textFromValue(self, value):
        text = super().textFromValue(value)
        decimal = self.locale().decimalPoint()
        if decimal in text:
            text = text.rstrip(self.locale().zeroDigit()).rstrip(decimal)
        return text


class SensitivityControls(QWidget):
    validity_changed = Signal(bool)

    def __init__(self, policy, parent=None):
        super().__init__(parent)
        self.original = deepcopy(policy)
        self.dirty = False
        self.valid = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel('表情灵敏度'))
        self.preset = QComboBox()
        self.preset.setObjectName('fx_sensitivity')
        for name, (label, *_) in SENSITIVITY_PRESETS.items():
            self.preset.addItem(label, name)
        self.preset.addItem('自定义', 'custom')
        self.preset.setCurrentIndex(self.preset.findData(sensitivity_name(policy)))
        row.addWidget(self.preset, 1)
        restore = QPushButton('恢复默认')
        restore.setObjectName('fx_sensitivity_restore')
        row.addWidget(restore)
        layout.addLayout(row)
        self.custom = QWidget()
        form = QFormLayout(self.custom)
        form.setContentsMargins(0, 0, 0, 0)
        self.spins = []
        for label, key in [('横盘阈值', 'flat_percent'), ('强波动阈值', 'strong_percent'),
                           ('恐慌跌幅', 'panic_drop_percent')]:
            spin = PercentSpinBox()
            spin.setObjectName('fx_' + key)
            spin.setRange(0., 1e9)
            spin.setDecimals(2)
            spin.setSingleStep(.01)
            spin.setSuffix(' %')
            spin.setValue(policy[key])
            form.addRow(label, spin)
            self.spins.append(spin)
        layout.addWidget(self.custom)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName('fx_sensitivity_summary')
        layout.addWidget(self.summary)
        layout.addWidget(QLabel('数值越小越敏感；账户亏损提醒线独立设置。'))
        self.preset.currentIndexChanged.connect(self._preset_changed)
        for spin in self.spins:
            spin.valueChanged.connect(self._edited)
        restore.clicked.connect(self.restore_default)
        self._refresh()

    def _preset_changed(self):
        self.dirty = True
        name = self.preset.currentData()
        if name in SENSITIVITY_PRESETS:
            for spin, value in zip(self.spins, SENSITIVITY_PRESETS[name][1:]):
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)
        self._refresh()

    def _edited(self):
        self.dirty = True
        self._refresh()

    def restore_default(self):
        self.preset.setCurrentIndex(self.preset.findData('default'))
        self._preset_changed()

    def policy(self):
        if not self.dirty:
            return deepcopy(self.original)
        values = sensitivity_values(*(spin.value() for spin in self.spins))
        return validated_policy({**self.original, **values})

    def _refresh(self):
        self.custom.setVisible(self.preset.currentData() == 'custom')
        try:
            policy = self.policy()
            self.summary.setText(
                f'横盘 {policy["flat_percent"]:g}% · 强波动 {policy["strong_percent"]:g}% · '
                f'恐慌跌幅 {policy["panic_drop_percent"]:g}%\n'
                f'退出阈值：{policy["flat_exit_percent"]:.6g}% / {policy["strong_exit_percent"]:.6g}%')
            self.summary.setStyleSheet('')
            self.valid = True
        except ValueError as error:
            self.summary.setText(str(error))
            self.summary.setStyleSheet('color: #e46d78')
            self.valid = False
        self.validity_changed.emit(self.valid)
