"""Desktop work areas, occupied space, and multi-monitor placement regressions."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QRect, QSize
from window_placement import dialog_rect


class PlacementTests(unittest.TestCase):
    def test_corner_and_central_pets_never_cover_dialog(self):
        area = QRect(0, 0, 1920, 1040)  # Taskbar is outside the work area.
        size = QSize(586, 720)
        for x, y in ((0, 0), (1600, 0), (0, 616), (1600, 616), (800, 300)):
            with self.subTest(x=x, y=y):
                pet = QRect(x, y, 320, 424)
                rect = dialog_rect(size, QSize(230, 150), pet, [area])
                self.assertEqual(rect.size(), size)
                self.assertTrue(area.contains(rect))
                self.assertFalse(rect.intersects(pet.adjusted(-16, -16, 16, 16)))

    def test_smaller_screen_uses_scrollable_dialog_in_free_space(self):
        area, pet = QRect(0, 0, 1280, 760), QRect(480, 200, 320, 424)
        rect = dialog_rect(QSize(586, 720), QSize(230, 150), pet, [area])
        self.assertTrue(area.contains(rect))
        self.assertFalse(rect.intersects(pet))
        self.assertGreaterEqual(rect.width(), 230)
        self.assertGreaterEqual(rect.height(), 150)

    def test_other_screen_before_shrinking_and_negative_coordinates(self):
        first, second = QRect(0, 0, 1280, 760), QRect(-1920, -120, 1920, 1040)
        pet, size = QRect(480, 200, 320, 424), QSize(586, 720)
        rect = dialog_rect(size, QSize(230, 150), pet, [first, second])
        self.assertEqual(rect.size(), size)
        self.assertTrue(second.contains(rect))
        self.assertFalse(rect.intersects(pet))

    def test_prefer_pets_screen_when_both_have_room(self):
        preferred, other = QRect(1920, 0, 1920, 1040), QRect(0, 0, 1920, 1040)
        rect = dialog_rect(QSize(586, 720), QSize(230, 150),
                           QRect(3400, 580, 320, 424), [preferred, other])
        self.assertTrue(preferred.contains(rect))

    def test_partial_offscreen_pet_and_small_warning_box(self):
        area = QRect(0, 0, 1280, 760)
        for pet in (QRect(-100, 120, 320, 424), QRect(1200, -100, 320, 424)):
            rect = dialog_rect(QSize(420, 180), QSize(230, 150), pet, [area])
            self.assertTrue(area.contains(rect))
            self.assertFalse(rect.intersects(pet))


if __name__ == '__main__':
    unittest.main(verbosity=2)
