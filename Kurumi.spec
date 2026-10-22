# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import json
from PyInstaller.utils.hooks import collect_dynamic_libs, get_package_paths
from pet_audio import SOUND_NAMES
from pet_core import public_default_config

root = Path(SPECPATH)
defaults = root / 'build' / 'bundled-defaults' / 'kurumi.json'
defaults.parent.mkdir(parents=True, exist_ok=True)
defaults.write_text(json.dumps(public_default_config(root/'kurumi.json'), ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
a = Analysis(
    [str(root / 'main.py')],
    pathex=[str(root)],
    binaries=collect_dynamic_libs('onnxruntime'),
    datas=[(str(root / 'assets' / f'kurumi_{i:02d}.png'), 'assets') for i in range(1, 15)]
          + [(str(root / 'assets' / 'sfx' / f'{name}.mp3'), 'assets/sfx') for name in SOUND_NAMES]
          + [(str(defaults), '.'), (str(root / 'LICENSES.txt'), '.'),
             (str(Path(get_package_paths('rapidocr_onnxruntime')[1]) / 'models' /
                  'ch_PP-OCRv4_rec_infer.onnx'), 'ocr_models')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtQml',
              'PySide6.QtQuick', 'PySide6.QtPdf', 'PySide6.QtNetwork', 'tkinter', 'unittest',
              'PIL.AvifImagePlugin', 'PIL.WebPImagePlugin', 'PIL.Jpeg2KImagePlugin'],
    noarchive=False,
    optimize=1,
)
# Qt 6.12 uses Windows' built-in ICU API. A non-system icuuc.dll found on
# PATH (e.g. from Poppler/Conda, with version-suffixed exports) is incompatible.
# Let the Windows loader resolve these OS components from System32.
a.binaries = [entry for entry in a.binaries
              if Path(entry[0]).name.lower() not in ('icuuc.dll', 'icuin.dll')
              and not Path(entry[0]).name.lower().startswith('icudt')]
# This is a raster QWidget app with PNG sprites, not a video/OpenGL/QML app.
# Keep the Windows platform/style plugins; discard optional format/video backends.
def needed_binary(entry):
    name = entry[0].replace('\\', '/').lower()
    base = name.rsplit('/', 1)[-1]
    if base.startswith('opencv_videoio_ffmpeg') or base == 'opengl32sw.dll':
        return False
    if name.startswith('pyside6/plugins/'):
        return name.endswith('/platforms/qwindows.dll') or name.endswith('/styles/qmodernwindowsstyle.dll')
    if name.startswith('pyside6/qt6') and base.endswith('.dll'):
        return base in ('qt6core.dll', 'qt6gui.dll', 'qt6widgets.dll')
    return True
a.binaries = [entry for entry in a.binaries if needed_binary(entry)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='Kurumi',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(root / 'assets' / 'kurumi.ico'),
)
