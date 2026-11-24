# -*- mode: python ; coding: utf-8 -*-
# Receita do PyInstaller: gera dist\NeoSD\NeoSD.exe (pasta única, abre rápido e
# é menos barrado por antivírus do que o modo "arquivo único").
# Use pelo build.bat; para rodar à mão:  pyinstaller NeoSD.spec --noconfirm
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

# informações que aparecem em Propriedades > Detalhes do .exe
nums = (VERSION.split(".") + ["0", "0", "0"])[:4]
(ROOT / "build").mkdir(exist_ok=True)
(ROOT / "build" / "version_info.txt").write_text(f"""
VSVersionInfo(
  ffi=FixedFileInfo(filevers=({",".join(nums)}), prodvers=({",".join(nums)}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('041604B0', [
      StringStruct('CompanyName', 'NeoSD'),
      StringStruct('FileDescription', 'NEO SD - launcher de jogos'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'NeoSD'),
      StringStruct('OriginalFilename', 'NeoSD.exe'),
      StringStruct('ProductName', 'NEO SD'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [0x0416, 1200])])]
)
""", encoding="utf-8")

datas = [(str(ROOT / "ui"), "ui"), (str(ROOT / "assets"), "assets"), (str(ROOT / "VERSION"), ".")]
datas += collect_data_files("webview")          # WebView2Loader.dll, scripts do pywebview
hidden = collect_submodules("webview.platforms") + collect_submodules("core") + [
    "clr", "clr_loader", "win32api", "win32file", "win32con", "pywintypes", "winreg",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "unittest", "pydoc", "test", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="NeoSD",
    icon=str(ROOT / "assets" / "neosd.ico"),
    version=str(ROOT / "build" / "version_info.txt"),
    console=False,          # sem janela preta de terminal
    upx=False,              # UPX costuma gerar falso positivo em antivírus
)
coll = COLLECT(exe, a.binaries, a.datas, name="NeoSD", upx=False)
