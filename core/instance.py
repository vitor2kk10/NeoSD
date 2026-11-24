"""Instância única: se o NeoSD já está aberto (ex.: o AnyFSE o chamou de novo), a 2ª execução só avisa a
primeira por um arquivo-sinal e sai. A primeira lê o sinal e traz a janela pra frente."""
import ctypes
import json
import os
import sys
import time

from .store import LIBRARY

WAKE = LIBRARY / "wake.json"
_mutex = None  # mantém o handle vivo enquanto o processo existir


def acquire():
    """True se esta é a primeira instância."""
    global _mutex
    if sys.platform != "win32":
        return True
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\NeoSD.SingleInstance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def signal_existing(console, path=WAKE):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"console": bool(console), "at": time.time()}, f)
    except OSError:
        pass


def poll_wake(path=WAKE):
    """-> {'console': bool} se alguém pediu atenção desde a última checagem (e consome o sinal), senão None."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        os.remove(path)
        return data
    except (OSError, ValueError):
        return None
