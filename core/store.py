"""Leitura/gravação dos JSONs (catalog.json, emulators.json) na pasta de dados (ver data_dir)."""
import json
import os
from pathlib import Path

import sys



def data_dir(frozen=None, exe=None, env=None):
    """Onde ficam os dados graváveis (catálogo, capas, configurações, cache).

    - Python normal: ao lado do main.py (como sempre foi).
    - Instalado (.exe do PyInstaller): em %LOCALAPPDATA%\\NeoSD, porque a pasta de instalação
      (Arquivos de Programas) não aceita gravação sem administrador.
    - Portátil: se existir um arquivo 'portable.txt' ao lado do .exe, os dados ficam ali mesmo
      (bom para levar num pendrive ou testar sem instalar).
    """
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen:
        return Path(__file__).resolve().parent.parent
    exe_dir = Path(exe or sys.executable).parent
    if (exe_dir / "portable.txt").exists():
        return exe_dir
    env = os.environ if env is None else env
    root = Path(env.get("LOCALAPPDATA") or env.get("APPDATA") or str(Path.home()))
    new, old = root / "NeoSD", root / "RetroSD"  # o projeto se chamava RetroSD: aproveita a biblioteca antiga
    if not new.exists() and old.is_dir():
        try:
            old.rename(new)
        except OSError:
            return old
    return new


BASE = data_dir()
try:
    BASE.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
LIBRARY = BASE / "library"


def resolve(path):
    """Caminhos relativos do catálogo (ex.: 'library/Outlast/cover.jpg') são
    resolvidos a partir da pasta do projeto, não do diretório atual."""
    if not path:
        return None
    p = Path(path)
    return p if p.is_absolute() else BASE / p


def load_json(name):
    try:
        with open(BASE / name, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_json(name, data):
    tmp = BASE / (name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, BASE / name)  # gravação atômica: nunca deixa o JSON pela metade
