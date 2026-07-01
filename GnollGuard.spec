# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Gnoll Guard desktop app.
Build with: pyinstaller GnollGuard.spec
"""

import os
from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None

# CustomTkinter ships theme JSON + assets that must be bundled or the app
# crashes at launch. collect_all grabs its data, binaries, and submodules.
_ctk_datas, _ctk_bins, _ctk_hidden = collect_all('customtkinter')
_pg_datas,  _pg_bins,  _pg_hidden  = collect_all('pygame')

a = Analysis(
    ['app/main.py'],
    pathex=['.'],
    binaries=_ctk_bins + _pg_bins,
    datas=[
        ('assets', 'assets'),
        ('config/settings.json', 'config'),
    ] + _ctk_datas + _pg_datas,
    hiddenimports=_ctk_hidden + _pg_hidden + [
        'customtkinter',
        'pystray',
        'pystray._win32',
        'PIL',
        'PIL._tkinter_finder',
        'watchdog.observers',
        'watchdog.observers.winapi',
        'watchdog.events',
        'sqlalchemy.dialects.sqlite',
        'sqlalchemy.pool',
        'bs4',
        'requests',
        'supabase',
        'httpx',
        'app.db.models',
        'app.db.queries',
        'app.db.export',
        'app.parsers.loot_parser',
        'app.parsers.npc_parser',
        'app.research.queue',
        'app.research.scraper',
        'app.alerts.engine',
        'app.alerts.window',
        'app.sync.supabase',
        'app.sync.auth',
        'app.ui.main_window',
        'app.ui.settings',
        'app.ui.theme',
        'app.updater',
        'app.version',
        'app.clipboard_watcher',
        'app.parsers.item_ocr',
        'winrt.windows.media.ocr',
        'winrt.windows.graphics.imaging',
        'winrt.windows.storage.streams',
        'pytesseract',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'transformers', 'ollama'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='GnollGuard',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # No console window — GUI only
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
    version_file=None,
)
