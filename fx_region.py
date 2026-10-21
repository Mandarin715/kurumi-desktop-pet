"""Screen-local price selection; the overlay never screenshots a screen."""
import json
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget


def region_data(screen, rect):
    g = screen.geometry()
    return {'screen': screen.name(), 'geometry': [g.x(), g.y(), g.width(), g.height()],
            'dpr': screen.devicePixelRatio(), 'rect': [rect.x(), rect.y(), rect.width(), rect.height()]}


def resolve_region(data):
    if not isinstance(data, dict):
        return None
    try:
        for screen in QApplication.screens():
            g = screen.geometry()
            if (screen.name() == data['screen'] and data['geometry'] == [g.x(), g.y(), g.width(), g.height()]
                    and abs(screen.devicePixelRatio()-data['dpr']) < .01):
                values = data['rect']
                if len(values) != 4 or any(type(n) is not int for n in values):
                    return None
                rect = QRect(*values)
                if rect.width() >= 12 and rect.height() >= 10 and QRect(QPoint(), g.size()).contains(rect):
                    return screen, rect
    except (KeyError, TypeError, ValueError):
        pass
    return None


def load_region(settings):
    try:
        return json.loads(settings.value('fx/region', 'null'))
    except (TypeError, ValueError):
        return None


class RegionOverlay(QWidget):
    selected = Signal(object)
    cancelled = Signal()

    def __init__(self, screen):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.screen_ref, self.origin, self.selection = screen, None, QRect()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setWindowTitle('框选价格 · Esc 取消')

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(12, 16, 26, 110))
        if not self.selection.isEmpty():
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            painter.fillRect(self.selection, QColor(0, 0, 0, 1))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setPen(QPen(QColor('#ffb1d6'), 2))
            painter.drawRect(self.selection.adjusted(0, 0, -1, -1))
        painter.fillRect(QRect(20, 20, min(760, self.width()-40), 88), QColor(22, 24, 35, 235))
        painter.setPen(Qt.GlobalColor.white)
        painter.setFont(QFont('Microsoft YaHei UI', 12))
        painter.drawText(QRect(36, 30, self.width()-72, 66), Qt.TextFlag.TextWordWrap,
                         '拖动框选一个完整价格数字，稍留边距。\n不要包含代码、涨跌幅或多个价格。Esc 取消；截图仅在本地内存处理。')

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.position().toPoint()
            self.selection = QRect(self.origin, self.origin)
        elif event.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()

    def mouseMoveEvent(self, event):
        if self.origin is not None:
            end = event.position().toPoint()
            self.selection = QRect(min(self.origin.x(), end.x()), min(self.origin.y(), end.y()),
                                   abs(end.x()-self.origin.x())+1, abs(end.y()-self.origin.y())+1).intersected(self.rect())
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.origin is not None:
            self.mouseMoveEvent(event)
            self.origin = None
            if self.selection.width() >= 12 and self.selection.height() >= 10:
                self.selected.emit(region_data(self.screen_ref, self.selection))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()


class RegionSelector:
    def __init__(self, callback):
        self.callback, self.finished = callback, False
        self.overlays = [RegionOverlay(screen) for screen in QApplication.screens()]
        for overlay in self.overlays:
            overlay.selected.connect(self.finish)
            overlay.cancelled.connect(lambda: self.finish(None))
            overlay.show()
            overlay.raise_()
        if self.overlays:
            self.overlays[0].activateWindow()
            self.overlays[0].setFocus()

    def finish(self, data):
        if self.finished:
            return
        self.finished = True
        for overlay in self.overlays:
            overlay.close()
            overlay.deleteLater()
        self.callback(data)
