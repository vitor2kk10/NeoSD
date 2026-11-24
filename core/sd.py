"""Cartão SD como 'cartucho': detecção de mídia, leitura dos arquivos e monitor.

Mesma lógica do protótipo (num. de série do volume pra distinguir inserção de
letra reservada por leitor de cartão), só separada da interface."""
import os
import shutil
import threading
import time

try:
    import win32api
    import win32file
except ImportError:  # fora do Windows (testes) o monitor simplesmente fica inerte
    win32api = win32file = None

_UNSEEN = "__unseen__"


def volume_serial(drive):
    """Nº de série do volume (existe só com mídia dentro) ou None."""
    if win32api is None:
        return None
    try:
        return win32api.GetVolumeInformation(drive)[1]
    except Exception:
        return None


def snapshot():
    """{letra: nº de série | None} das unidades removíveis agora (None = leitor sem cartão)."""
    if win32api is None:
        return {}
    result = {}
    for drive in win32api.GetLogicalDriveStrings().split("\000")[:-1]:
        try:
            if win32file.GetDriveType(drive) != win32file.DRIVE_REMOVABLE:
                continue
        except Exception:
            continue
        result[drive] = volume_serial(drive)
    return result


def mounted_media():
    """Volumes removíveis com mídia AGORA: [{drive, serial, name}] (name = nome.txt, '' se não tem).
    É a base pra saber qual cartão está de fato inserido, independente da letra."""
    return [{"drive": d, "serial": s, "name": _read_text(os.path.join(d, "nome.txt"))}
            for d, s in snapshot().items() if s is not None]


def diff_media(known, current):
    """Compara dois snapshots -> (inseridos, removidos)."""
    inserted, removed = [], []
    for drive, serial in current.items():
        prev = known.get(drive, _UNSEEN)
        if serial is not None and serial != prev:
            inserted.append(drive)
        elif serial is None and prev not in (None, _UNSEEN):
            removed.append(drive)
    for drive, serial in known.items():  # pendrive que sumiu de vez
        if drive not in current and serial is not None:
            removed.append(drive)
    return inserted, removed


def _read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return default


def _first(drive, names):
    return next((os.path.join(drive, n) for n in names if os.path.exists(os.path.join(drive, n))), None)


def read_cartridge(drive, defaults=True):
    """Lê game.exe|game.bat, nome.txt, sinopse.txt, cover.*, icon.* da raiz. None se não for um cartucho.
    defaults=False deixa nome/sinopse vazios quando os .txt não existem (usado ao pré-preencher o formulário)."""
    exe = _first(drive, ("game.exe", "game.bat"))
    if not exe:
        return None
    serial = volume_serial(drive)
    txt_name = _read_text(os.path.join(drive, "nome.txt"))
    # sem nome.txt, o nº de série mantém cartões diferentes como jogos diferentes
    unknown = "Jogo Desconhecido" + (f" {serial & 0xFFFFFFFF:08X}" if serial is not None else "")
    return {
        "drive": drive,
        "serial": serial,
        "txt_name": txt_name,
        "exe": exe,
        "name": txt_name or (unknown if defaults else ""),
        "description": _read_text(os.path.join(drive, "sinopse.txt")) or ("Sem descrição" if defaults else ""),
        "cover": _first(drive, ("cover.jpg", "cover.png")),
        "icon": _first(drive, ("icon.png", "icon.jpg")),
    }


def find_cartridge():
    """Primeira unidade removível com um jogo na raiz (botão 'Detectar cartão SD' do formulário)."""
    for drive in snapshot():
        cart = read_cartridge(drive, defaults=False)
        if cart:
            return cart
    return None


def disk_info(drive):
    """Dados pro selo de armazenamento da interface; None se o cartão não responde."""
    try:
        u = shutil.disk_usage(drive)
    except OSError:
        return None
    free = u.free / 1024 ** 3
    return {"drive": drive.rstrip("\\/"),
            "free": f"{free:.0f} GB" if free >= 10 else f"{free:.1f} GB",
            "used": (u.total - u.free) / u.total if u.total else 0}


class SDMonitor(threading.Thread):
    """Checa as unidades a cada 2s e avisa via callbacks:
    on_insert(drive), on_remove([drives]), on_tick(drive_ativo | None)."""

    def __init__(self, on_insert, on_remove, on_tick, interval=2.0):
        super().__init__(daemon=True)
        self.on_insert, self.on_remove, self.on_tick, self.interval = on_insert, on_remove, on_tick, interval

    def run(self):
        known = {}
        while True:
            time.sleep(self.interval)
            try:
                current = snapshot()
                inserted, removed = diff_media(known, current)
                for d in inserted:
                    self.on_insert(d)
                if removed:
                    self.on_remove(removed)
                active = [d for d, s in current.items() if s is not None]
                self.on_tick(active[0] if active else None)
                known = current
            except Exception as e:  # o monitor nunca pode morrer
                print("[SDMonitor]", e)
