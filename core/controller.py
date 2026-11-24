"""Atalho global do controle (XInput): Start + Back juntos abrem o Modo Console,
mesmo com o launcher em segundo plano (a Gamepad API do navegador só funciona com foco)."""
import ctypes
import threading
import time


class _Gamepad(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class _State(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", _Gamepad)]


START, BACK = 0x0010, 0x0020


def _load_dll():
    for name in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
        try:
            return ctypes.windll.LoadLibrary(name)
        except Exception:
            continue
    return None


def pressed_buttons(dll):
    """Botões do primeiro controle conectado (0-3), ou None se não há nenhum."""
    for index in range(4):
        state = _State()
        if dll.XInputGetState(index, ctypes.byref(state)) == 0:
            return state.Gamepad.wButtons
    return None


def watch_combo(on_combo):
    dll = _load_dll()
    if dll is None:
        return

    def loop():
        was = False
        while True:
            time.sleep(0.05)
            buttons = pressed_buttons(dll)
            both = buttons is not None and (buttons & START) and (buttons & BACK)
            if both and not was:
                on_combo()
            was = bool(both)

    threading.Thread(target=loop, daemon=True).start()
