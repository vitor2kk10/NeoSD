"""Jogos em DISCO físico, no leitor de CD/DVD do PC: colocar o disco e jogar, como num console.

Reconhecer:
  - O Windows já monta o disco (D:\\, E:\\…), então o NeoSD lê o SYSTEM.CNF como um arquivo comum.
    PS2: "BOOT2 = cdrom0:\\SLUS_211.34;1"   PS1: "BOOT = cdrom:\\SLUS_000.67;1"  (sem SYSTEM.CNF: PSX.EXE = PS1)
  - O código (SLUS-21134) vira o nome do jogo pelo banco de dados do próprio emulador do RetroBat:
    PS2 = emulators\\pcsx2\\resources\\GameIndex.yaml (12 mil jogos); PS1 = banco do DuckStation, se estiver instalado.
    Sem banco: usa o rótulo do disco.
  - Discos de Saturn/Sega CD são reconhecidos pelo cabeçalho, mas os emuladores deles não leem do leitor do PC.
Jogar (emuladores do RetroBat, com as configurações que o RetroBat já gravou neles: BIOS, controle, tela cheia):
  - PS2: pcsx2-qt.exe -disc D:\\ -fullscreen -batch   ("-disc: uses the specified host DVD drive as a source")
  - PS1: DuckStation com o caminho de dispositivo \\\\.\\D: (instalável pelo menu de emuladores do RetroBat).
"""
import ctypes
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("neosd.discs")
DRIVE_CDROM = 5
SYSTEMS = {"ps2": "PlayStation 2", "psx": "PlayStation", "saturn": "Sega Saturn", "segacd": "Sega CD"}
RB_NAMES = {"ps2": "Sony PlayStation 2", "psx": "Sony PlayStation", "saturn": "Sega Saturn", "segacd": "Sega CD"}
PLAYABLE = ("ps2", "psx")
SERIAL_RE = re.compile(r"([A-Z]{4})[_\-]?(\d{3})\.?(\d{2})", re.I)


# ---------- leitor e disco ----------
def optical_drives():
    """['D:\\\\', …] — unidades de CD/DVD/Blu-ray, com ou sem disco."""
    if os.name != "nt":
        return []
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    return [f"{chr(65 + i)}:\\" for i in range(26) if mask >> i & 1 and k32.GetDriveTypeW(f"{chr(65 + i)}:\\") == DRIVE_CDROM]


def media(drive):
    """(rótulo, nº de série do volume) do disco no leitor, ou None se estiver vazio."""
    if os.name != "nt":
        return None
    label, serial = ctypes.create_unicode_buffer(261), ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(drive), label, 261, ctypes.byref(serial),
                                                      None, None, None, 0)
    return (label.value, f"{serial.value:08X}") if ok else None


def serial_from_exe(exe):
    m = SERIAL_RE.search(exe or "")
    return f"{m.group(1).upper()}-{m.group(2)}{m.group(3)}" if m else ""


def parse_cnf(text):
    """-> (sistema, executável) a partir do SYSTEM.CNF."""
    for line in text.splitlines():
        k, _, v = line.partition("=")
        k = k.strip().upper()
        if k in ("BOOT2", "BOOT"):
            v = v.strip()
            v = v.split(":", 1)[1] if ":" in v else v
            return ("ps2" if k == "BOOT2" else "psx"), v.lstrip("\\/").split(";")[0].strip()
    return None, ""


def _raw_header(drive):
    """Primeiro setor do disco (2048 bytes) para reconhecer Saturn/Sega CD."""
    try:
        with open(r"\\.\%s" % drive.rstrip("\\/"), "rb", buffering=0) as f:
            return f.read(2048)
    except OSError:
        return b""


# ---------- bancos de dados de nomes ----------
class GameDB:
    """Lê 'CODIGO:' + 'name:' de bancos em YAML (GameIndex.yaml do PCSX2, gamedb do DuckStation) sem precisar de
    uma biblioteca de YAML. Guarda em memória enquanto o arquivo não mudar."""

    def __init__(self):
        self._cache = {}

    def load(self, path):
        p = Path(path)
        try:
            mt = p.stat().st_mtime
        except OSError:
            return {}
        hit = self._cache.get(str(p))
        if hit and hit[0] == mt:
            return hit[1]
        out, cur = {}, None
        key_re = re.compile(r'^"?([A-Z]{4}-\d{5})"?\s*:\s*$')
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = key_re.match(line)
                if m:
                    cur = out.setdefault(m.group(1), {})
                    continue
                if cur is None or not line.startswith((" ", "\t")):
                    if not line.startswith((" ", "\t", "#")) and line.strip():
                        cur = None
                    continue
                k, _, v = line.strip().partition(":")
                if k in ("name", "name-en", "title", "region") and k not in cur:
                    cur[k] = v.strip().strip('"').strip("'")
        self._cache[str(p)] = (mt, out)
        return out


class DiscService:
    def __init__(self, retrobat, settings=None, runner=None, log_dir=None):
        self.rb, self.settings, self.log_dir = retrobat, settings, log_dir
        self.db = GameDB()
        self._popen = runner or subprocess.Popen
        self.on_launch = lambda system, drive, proc: None  # ex.: RetroAchievements durante o jogo
        self.current = {}  # leitor -> o que foi reconhecido quando o disco entrou (a tela consulta isto, sem girar o disco)

    def insert(self, drive):
        info = self.identify(drive)
        if info:
            self.current[drive] = info
        else:
            self.current.pop(drive, None)
        return info

    def remove(self, drive):
        self.current.pop(drive, None)

    # ---------- emuladores e bancos do RetroBat ----------
    def _emu_dir(self, name):
        root = self.rb.root() if self.rb else None
        return Path(root) / "emulators" / name if root else None

    def pcsx2(self):
        d = self._emu_dir("pcsx2")
        exe = d / "pcsx2-qt.exe" if d else None
        return exe if exe and exe.is_file() else None

    def duckstation(self):
        d = self._emu_dir("duckstation")
        if not d or not d.is_dir():
            return None
        exes = [e for e in d.glob("duckstation*.exe") if "updater" not in e.name.lower()]
        return sorted(exes, key=lambda e: ("qt" not in e.name.lower(), e.name))[0] if exes else None

    def _dbs(self, system):
        if system == "ps2":
            d = self._emu_dir("pcsx2")
            return [d / "resources" / "GameIndex.yaml"] if d else []
        d = self._emu_dir("duckstation")
        return [d / "resources" / n for n in ("gamedb.yaml", "gamedb.json")] if d else []

    def lookup(self, system, serial):
        for p in self._dbs(system):
            e = self.db.load(p).get(serial)
            if e:
                return e
        return {}

    # ---------- reconhecer ----------
    def identify(self, drive):
        """-> dict com drive, label, vserial, system, serial, name, region, playable, why."""
        m = media(drive)
        if not m:
            return None
        label, vserial = m
        info = {"drive": drive, "label": label, "vserial": vserial, "system": "", "serial": "", "name": "",
                "region": "", "playable": False, "why": ""}
        system, exe = None, ""
        try:
            system, exe = parse_cnf(Path(drive, "SYSTEM.CNF").read_text(encoding="latin-1", errors="replace"))
        except OSError:
            if Path(drive, "PSX.EXE").is_file():
                system = "psx"
        if not system:
            head = _raw_header(drive)
            if head.startswith(b"SEGA SEGASATURN"):
                system = "saturn"
            elif head.startswith(b"SEGADISCSYSTEM") or head[16:30] == b"SEGADISCSYSTEM":
                system = "segacd"
        if not system:
            info["why"] = ("Este disco não parece ser de um videogame (PC, filme ou música?)."
                           if label or Path(drive, "autorun.inf").exists() else "Não consegui ler o disco.")
            return info
        serial = serial_from_exe(exe)
        db = self.lookup(system, serial) if serial else {}
        pretty = re.sub(r"\s+", " ", (label or "").replace("_", " ")).strip()
        info.update(system=system, serial=serial, region=db.get("region", ""),
                    name=db.get("name-en") or db.get("name") or db.get("title") or pretty or serial or "Disco sem nome")
        if system not in PLAYABLE:
            info["why"] = (f"Disco de {SYSTEMS[system]}: os emuladores dele não leem direto do leitor do PC. "
                           "Para jogar, copie o disco para uma imagem (.cue/.bin) e coloque na pasta do RetroBat.")
        elif system == "ps2" and not self.pcsx2():
            info["why"] = "O PCSX2 do RetroBat não está instalado (Configurações → Emulação → Emuladores)."
        elif system == "psx" and not self.duckstation():
            info.update(why="Para jogar discos de PS1 direto do leitor, instale o DuckStation pelo RetroBat.",
                        need="duckstation")
        else:
            info["playable"] = True
        return info

    def find(self, game):
        """Leitor com o disco DESTE jogo agora (pelo código do jogo; sem código, pelo nº de série do volume)."""
        for d, info in list(self.current.items()):
            if game.get("disc_serial") and info.get("serial") == game["disc_serial"]:
                return d
            if not game.get("disc_serial") and game.get("disc_vserial") and info.get("vserial") == game["disc_vserial"]:
                return d
        return None

    # ---------- jogar ----------
    def command(self, system, drive):
        letter = drive.rstrip("\\/")  # "D:"
        if system == "ps2":
            # O PCSX2 confere o leitor com GetDriveType("D:\\") e depois tira a última letra para abrir "\\.\D:".
            # Com "D:" (sem a barra) ele abria "\\.\D" e dava "CDVD: drive não encontrado". Tem que ser "D:\\".
            exe = self.pcsx2()
            if not exe:
                return None
            cmd = [str(exe), "-disc", letter + "\\", "-fullscreen", "-batch"]
            if self.log_dir:  # registro do PCSX2 só deste disco, para investigar se o jogo não abrir
                cmd[1:1] = ["-logfile", str(Path(self.log_dir) / "pcsx2-disco.log")]
            return cmd
        if system == "psx":
            exe = self.duckstation()
            return [str(exe), "-fullscreen", "-batch", "--", "\\\\.\\" + letter] if exe else None
        return None

    def launch(self, system, drive):
        cmd = self.command(system, drive)
        if not cmd:
            return False, ("Instale o DuckStation pelo RetroBat para jogar discos de PS1." if system == "psx" else
                           "O emulador deste disco não está instalado no RetroBat.")
        log.info("disco: %s", subprocess.list2cmdline(cmd))
        try:
            proc = self._popen(cmd, cwd=str(Path(cmd[0]).parent))
        except OSError as e:
            return False, f"Não consegui abrir o emulador: {e}"
        try:
            self.on_launch(system, drive, proc)
        except Exception:
            log.exception("depois de abrir o disco")
        return True, ""


class DiscMonitor(threading.Thread):
    """Olha os leitores a cada 2 s: on_insert(drive) quando entra um disco, on_remove(drive) quando sai."""

    def __init__(self, on_insert, on_remove, interval=2.0):
        super().__init__(daemon=True, name="discs")
        self.on_insert, self.on_remove, self.interval = on_insert, on_remove, interval

    def run(self):
        known = {}  # na primeira volta, um disco que já estava no leitor também conta como "inserido"
        while True:
            try:
                now = {d: media(d) for d in optical_drives()}
                for d, m in now.items():
                    if m and m != known.get(d):
                        self.on_insert(d)
                    elif not m and known.get(d):
                        self.on_remove(d)
                known = now
            except Exception:
                log.exception("monitor de discos")
            time.sleep(self.interval)
