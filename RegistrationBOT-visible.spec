# -*- mode: python ; coding: utf-8 -*-
import os
from importlib.util import find_spec

_ddddocr_dir = os.path.dirname(find_spec('ddddocr').origin)
_selenium_dir = os.path.dirname(find_spec('selenium').origin)
_selenium_manager = os.path.join(
    _selenium_dir,
    'webdriver',
    'common',
    'windows',
    'selenium-manager.exe',
)

a = Analysis(
    ['register_visible.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Automatic CAPTCHA OCR is needed when visible mode continues automatically.
        (os.path.join(_ddddocr_dir, 'common_old.onnx'), 'ddddocr'),
        (_selenium_manager, 'selenium/webdriver/common/windows'),
    ],
    hiddenimports=[
        'ddddocr',
        'onnxruntime',
        'PIL',
        'PIL.Image',
        'truststore',
        'truststore._api',
        'truststore._ssl_constants',
        'truststore._windows',
        'selenium.webdriver.chrome.webdriver',
        'selenium.webdriver.chrome.service',
        'selenium.webdriver.common.selenium_manager',
        'webdriver_manager.chrome',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'cv2',
        'opencv_python',
        'opencv_python_headless',
        'onnxruntime_tools',
        'matplotlib',
        'numpy',
        'pandas',
        'tkinter',
        'unittest',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='registrationscript-visible',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
