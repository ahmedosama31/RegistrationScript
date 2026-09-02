# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from importlib.util import find_spec

_selenium_dir = os.path.dirname(find_spec('selenium').origin)
_selenium_manager_platform = {
    'darwin': 'macos',
    'win32': 'windows',
}.get(sys.platform, 'linux')
_selenium_manager_filename = (
    'selenium-manager.exe' if sys.platform == 'win32' else 'selenium-manager'
)
_selenium_manager = os.path.join(
    _selenium_dir,
    'webdriver',
    'common',
    _selenium_manager_platform,
    _selenium_manager_filename,
)
_truststore_backend = {
    'darwin': 'truststore._macos',
    'win32': 'truststore._windows',
}.get(sys.platform, 'truststore._openssl')

a = Analysis(
    ['register.py'],
    pathex=[],
    binaries=[],
    datas=[
        (_selenium_manager, f'selenium/webdriver/common/{_selenium_manager_platform}'),
    ],
    hiddenimports=[
        'PIL',
        'PIL.Image',
        'truststore',
        'truststore._api',
        'truststore._ssl_constants',
        _truststore_backend,
        'selenium.webdriver.chrome.webdriver',
        'selenium.webdriver.chrome.service',
        'selenium.webdriver.common.selenium_manager',
        'webdriver_manager.chrome',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'ddddocr',
        'onnxruntime',
        'onnxruntime_tools',
        'cv2',
        'opencv_python',
        'opencv_python_headless',
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
    # Keep the release filename stable and aligned with the repository name.
    name='registrationscript-lite',
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
