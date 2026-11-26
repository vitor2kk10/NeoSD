"""Mantém a janela do modo console cobrindo a tela inteira.

Problema visto no PC de testes ligado no "modo Xbox" (experiência de tela cheia do Windows + AnyFSE): o NeoSD
abria, mas não em tela cheia. Causas possíveis, todas tratadas aqui:
  - a TV/monitor troca de resolução logo depois do login (ex.: 1080p -> 4K/HDR) e a janela fica com o tamanho antigo;
  - o AnyFSE (ou o atalho Start+Back) abre o NeoSD de novo: a 2ª execução pedia para a 1ª "voltar" com
    window.restore(), que no Windows tira a janela do estado maximizado — e a tela cheia do pywebview é uma janela
    sem borda MAXIMIZADA;
  - a janela é criada antes de o Windows terminar de montar a tela.
Enquanto o modo console estiver ligado, um vigia confere a cada 1,5 s se a janela cobre o monitor dela; se não
cobrir, ajusta posição e tamanho SEM mudar a ordem das janelas (um jogo aberto continua na frente).
"""
import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes

log = logging.getLogger("neosd.fullscreen")

GWL_STYLE = -16
WS_CAPTION, WS_THICKFRAME = 0x00C00000, 0x00040000
SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_NOOWNERZORDER = 0x0004, 0x0010, 0x0020, 0x0200
SW_RESTORE = 9
MONITOR_DEFAULTTONEAREST = 2


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def _user32():
    u = ctypes.windll.user32
    vp = ctypes.c_void_p
    u.MonitorFromWindow.argtypes, u.MonitorFromWindow.restype = [vp, wintypes.DWORD], vp
    u.GetMonitorInfoW.argtypes, u.GetMonitorInfoW.restype = [vp, ctypes.POINTER(MONITORINFO)], wintypes.BOOL
    u.GetWindowRect.argtypes, u.GetWindowRect.restype = [vp, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL
    u.IsIconic.argtypes = u.IsWindowVisible.argtypes = u.SetForegroundWindow.argtypes = [vp]
    u.ShowWindow.argtypes = [vp, ctypes.c_int]
    u.SetWindowPos.argtypes = [vp, vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    get = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
    put = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
    get.argtypes, get.restype = [vp, ctypes.c_int], ctypes.c_ssize_t
    put.argtypes, put.restype = [vp, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t
    return u, get, put


class FullscreenGuard:
    def __init__(self, window, is_full, interval=1.5):
        self.window, self.is_full, self.interval = window, is_full, interval
        self._u = None
        self.fixes = 0

    def hwnd(self):
        try:
            h = self.window.native.Handle
            return int(h.ToInt64()) if hasattr(h, "ToInt64") else int(h)
        except Exception:
            return None

    def _rects(self, hwnd):
        u = self._u[0]
        mon = u.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not mon or not u.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None, None
        wr = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(wr))
        m = mi.rcMonitor
        return (m.left, m.top, m.right, m.bottom), (wr.left, wr.top, wr.right, wr.bottom)

    def fix(self, why="vigia"):
        """Ajusta a janela para cobrir o monitor. -> True se precisou mexer."""
        if os.name != "nt" or not self.is_full():
            return False
        hwnd = self.hwnd()
        if not hwnd:
            return False
        if self._u is None:
            self._u = _user32()
        u, get, put = self._u
        if u.IsIconic(hwnd) or not u.IsWindowVisible(hwnd):  # minimizada de propósito (ex.: instalador aberto)
            return False
        mon, win = self._rects(hwnd)
        if not mon:
            return False
        style = get(hwnd, GWL_STYLE)
        framed = bool(style & (WS_CAPTION | WS_THICKFRAME))
        if win == mon and not framed:
            return False
        if framed:
            put(hwnd, GWL_STYLE, style & ~(WS_CAPTION | WS_THICKFRAME))
        u.SetWindowPos(hwnd, None, mon[0], mon[1], mon[2] - mon[0], mon[3] - mon[1],
                       SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_NOOWNERZORDER)
        self.fixes += 1
        if self.fixes <= 20:  # registra as primeiras para dar para investigar (sem encher o log)
            log.info("tela cheia ajustada (%s): janela %s -> monitor %s%s", why, win, mon, " (tinha borda)" if framed else "")
        return True

    def front(self):
        """Traz o NeoSD para a frente em tela cheia (no lugar de window.restore(), que desfazia a tela cheia)."""
        hwnd = self.hwnd()
        if not hwnd or os.name != "nt":
            return
        if self._u is None:
            self._u = _user32()
        u = self._u[0]
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, SW_RESTORE)
        self.fix("trazer para a frente")
        u.SetForegroundWindow(hwnd)

    def start(self):
        def loop():
            first = time.monotonic()
            while True:
                try:
                    self.fix("início" if time.monotonic() - first < 30 else "vigia")
                except Exception:
                    log.exception("vigia da tela cheia")
                time.sleep(self.interval)
        threading.Thread(target=loop, daemon=True, name="fullscreen-guard").start()
