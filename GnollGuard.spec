# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Gnoll Guard desktop app.
Build with: pyinstaller GnollGuard.spec
"""

import os
import re
from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None

# ── Embed a REAL Windows version resource ────────────────────────────────────
#
# 🔴 Until 2026-08-16 this spec passed `version_file=None`, so no GnollGuard.exe we
# have ever shipped carried a version resource. `(Get-Item gg.exe).VersionInfo
# .ProductVersion` returned an EMPTY STRING for every build.
#
# That matters more than it sounds. After v1.5.15 and v1.5.16 both shipped the wrong
# binary under the right tag, CLAUDE.md added exactly that command as the guard that
# would catch it happening again — and the guard could never have worked. It reported
# empty for a correct build and for a wrong build alike. v1.5.23 was very nearly
# advertised on the strength of a check that cannot fail or pass.
#
# The version is READ FROM app/version.py at build time, never typed here. A literal
# in this file would just be a second place to forget, which is the whole failure mode
# we are trying to close.
_ver_src = open(os.path.join('app', 'version.py'), encoding='utf-8').read()
_VERSION = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', _ver_src).group(1)
_parts = [int(x) for x in _VERSION.split('.')]
while len(_parts) < 4:            # Windows VERSIONINFO is always a 4-tuple
    _parts.append(0)
_VTUPLE = tuple(_parts[:4])

_VERSION_FILE = os.path.join('build', 'file_version_info.txt')
os.makedirs('build', exist_ok=True)
with open(_VERSION_FILE, 'w', encoding='utf-8') as _fh:
    _fh.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_VTUPLE},
    prodvers={_VTUPLE},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Gnoll Guard'),
        StringStruct('FileDescription', 'Gnoll Guard - EverQuest Legends companion'),
        StringStruct('FileVersion', '{_VERSION}'),
        StringStruct('InternalName', 'GnollGuard'),
        StringStruct('OriginalFilename', 'GnollGuard.exe'),
        StringStruct('ProductName', 'Gnoll Guard'),
        StringStruct('ProductVersion', '{_VERSION}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")
print(f"[spec] embedding version resource {_VERSION} {_VTUPLE}")

# CustomTkinter ships theme JSON + assets that must be bundled or the app
# crashes at launch. collect_all grabs its data, binaries, and submodules.
# pygame was only used for broken alert sounds — no longer shipped.
_ctk_datas, _ctk_bins, _ctk_hidden = collect_all('customtkinter')

def _ctk_snapshots():
    """Reference snapshots for the Gear and Codex tabs, bundled ONLY if present.

    🔴 THEY ARE GITIGNORED ON PURPOSE. This repo is public and the database exports must
    never enter it. But CI builds from the repo, so a hard `('build_data/items.json', ...)`
    entry fails the tagged build outright:

        ERROR: Unable to find 'D:\a\Gnollsfoot\Gnollsfoot\build_data\items.json'

    Bundling a gitignored file works on the maintainer's machine and can NEVER work in CI.
    So this is opportunistic: a local build that has build_data/ ships the snapshots, and a
    CI build without them still produces a working exe. `datapaths` already prefers a
    user-updated copy in %LOCALAPPDATA% and degrades to a clear on-screen notice, so the
    difference is which tabs have data -- never whether the app runs.
    """
    out = []
    for name in ("items.json", "mobs.json", "exaltations.json"):
        p = os.path.join("build_data", name)
        if os.path.exists(p):
            out.append((p, "data"))
    print("[spec] bundling %d/3 reference snapshots" % len(out))
    return out


a = Analysis(
    ['app/main.py'],
    pathex=['.'],
    binaries=_ctk_bins,
    datas=[
        ('assets', 'assets'),
        ('config/settings.json', 'config'),
    ] + _ctk_snapshots() + _ctk_datas,
    hiddenimports=_ctk_hidden + [
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
        'app.alerts.engine',
        'app.sync.supabase',
        'app.sync.auth',
        'app.ui.main_window',
        'app.ui.settings',
        'app.ui.theme',
        'app.ui.journal_overlay',
        'app.ui.journal_view',
        'app.updater',
        'app.version',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'transformers', 'ollama', 'pygame'],
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
    # 🔴 The kwarg is `version`, NOT `version_file`. PyInstaller's EXE() swallows unknown
    # kwargs without a word, so `version_file=None` sat here for the life of the project
    # looking exactly like a deliberate switch and doing absolutely nothing. Renaming it
    # to `version_file=<path>` produced a build that printed "embedding version resource"
    # and still shipped an empty ProductVersion. Verified against the real signature:
    # PyInstaller reads 'version'; 'version_file' is not in the list it ever looks at.
    version=_VERSION_FILE,   # generated above FROM app/version.py — never hand-edited
)
