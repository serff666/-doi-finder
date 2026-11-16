# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    datas=[('web', 'web')],
    hiddenimports=['pypdf','appdata','regex','tqdm','charset_normalizer'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch','torchvision','torchaudio','pyarrow','onnxruntime','scipy','pandas',
              'matplotlib','sklearn','jedi','IPython','notebook','argostranslate',
              'ctranslate2','sentencepiece','stanza'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DOI Finder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DOI Finder',
)


