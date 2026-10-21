"""Place framed dialogs in desktop work areas without covering the pet."""
from PySide6.QtCore import QRect, QSize
from PySide6.QtGui import QGuiApplication


def dialog_rect(size, minimum, obstacle, work_areas, gap=16):
    """Prefer full size on the pet's screen; shrink only when no screen fits."""
    spaces = []
    for priority, area in enumerate(work_areas):
        blocked = obstacle.adjusted(-gap, -gap, gap, gap).intersected(area)
        if blocked.isEmpty():
            free = [area]
        else:
            free = [QRect(area.left(), area.top(), blocked.left()-area.left(), area.height()),
                    QRect(blocked.right()+1, area.top(), area.right()-blocked.right(), area.height()),
                    QRect(area.left(), area.top(), area.width(), blocked.top()-area.top()),
                    QRect(area.left(), blocked.bottom()+1, area.width(), area.bottom()-blocked.bottom())]
        spaces.extend((priority, rect) for rect in free if not rect.isEmpty())

    def place(space, wanted):
        width, height = min(space.width(), wanted.width()), min(space.height(), wanted.height())
        x = max(space.left(), min(obstacle.center().x()-width//2, space.right()-width+1))
        y = max(space.top(), min(obstacle.center().y()-height//2, space.bottom()-height+1))
        return QRect(x, y, width, height)

    def distance(rect):
        delta = rect.center()-obstacle.center()
        return delta.x()**2+delta.y()**2

    full = [(priority, place(space, size)) for priority, space in spaces
            if space.width() >= size.width() and space.height() >= size.height()]
    if full:
        return min(full, key=lambda item: (item[0], distance(item[1])))[1]
    smaller = [(priority, place(space, size)) for priority, space in spaces
               if space.width() >= minimum.width() and space.height() >= minimum.height()]
    if smaller:
        return min(smaller, key=lambda item: (-item[1].width()*item[1].height(),
                                              item[0], distance(item[1])))[1]
    # On an exceptionally small desktop there may be no legal non-overlapping
    # placement. Keep controls on screen and choose the least overlap possible.
    candidates = []
    for priority, area in enumerate(work_areas):
        fitted = size.boundedTo(area.size())
        for x in (area.left(), area.right()-fitted.width()+1):
            for y in (area.top(), area.bottom()-fitted.height()+1):
                rect = QRect(x, y, fitted.width(), fitted.height())
                overlap = rect.intersected(obstacle)
                candidates.append((overlap.width()*overlap.height(), priority, distance(rect), rect))
    return min(candidates, key=lambda item: item[:3])[3] if candidates else QRect(obstacle.topLeft(), size)


def place_dialog(dialog, pet):
    dialog.ensurePolished()
    if dialog.layout():
        dialog.layout().activate()
    # Create the native handle while still hidden, so title bar and border
    # margins participate in placement before the first visible frame.
    dialog.winId()
    margins = dialog.windowHandle().frameMargins()
    frame = QSize(margins.left()+margins.right(), margins.top()+margins.bottom())
    minimum = dialog.minimumSizeHint().expandedTo(dialog.minimumSize())
    preferred = QGuiApplication.screenAt(pet.frameGeometry().center()) or pet.screen()
    screens = [preferred]+[screen for screen in QGuiApplication.screens() if screen != preferred]
    screens = [screen for screen in screens if screen is not None]
    rect = dialog_rect(dialog.size().expandedTo(minimum)+frame, minimum+frame,
                       pet.frameGeometry(), [screen.availableGeometry() for screen in screens])
    dialog.resize(rect.size()-frame)
    dialog.move(rect.topLeft())
