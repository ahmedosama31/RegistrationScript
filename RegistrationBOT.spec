# -*- mode: python ; coding: utf-8 -*-
import os
import ddddocr

# Locate the ddddocr package directory so we can bundle its model file.
_ddddocr_dir = os.path.dirname(ddddocr.__file__)

a = Analysis(
    ['register.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Bundle the ddddocr ONNX model and any other data files it ships with.
        (_ddddocr_dir, 'ddddocr'),
    ],
    hiddenimports=[
        'ddddocr',
        'onnxruntime',
        'PIL',
        'PIL.Image',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='RegistrationBOT',
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
