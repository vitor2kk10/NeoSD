"""Configurar os emuladores pelo NeoSD.

Por que as mudanças feitas no próprio emulador "somem": a cada jogo, o emulatorLauncher do RetroBat REESCREVE o
arquivo de configuração do emulador (PCSX2.ini, config.yml do RPCS3, settings.xml do Cemu…) com as opções do RetroBat.
Então há dois jeitos de configurar, e o NeoSD oferece os dois:

1. Opções do RetroBat (as mesmas do menu "Opções avançadas" da interface do RetroBat). A lista vem do próprio
   RetroBat (emulationstation\\.emulationstation\\es_features.cfg: ~4.700 opções de ~150 emuladores, com a tradução
   pt_BR de es_features.locale) e é gravada em es_settings.cfg:
       sistema:  ps2.internalresolution = 3
       jogo:     ps2["Resident Evil 4.iso"].internalresolution = 4     (o jogo vence o sistema)
   O emulatorLauncher aplica essas opções sempre, então elas nunca se perdem.

2. Iniciar DIRETO no emulador (sem o RetroBat): o NeoSD abre o pcsx2-qt.exe / rpcs3.exe / Cemu.exe… com o jogo, e o
   emulador usa as configurações dele mesmo, feitas na janela dele ("Abrir o emulador para configurar"). Nada é
   reescrito. Por sistema (Configurações → Emulação) ou por jogo.
"""
import glob
import logging
import os
import re
import subprocess
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

log = logging.getLogger("neosd.emuconfig")

# ----------------------------------------------------------------------------- iniciar direto no emulador
# pasta dentro de RetroBat\emulators, executáveis (glob, em ordem) e argumentos. {rom} = caminho do jogo.
DIRECT = {
    "pcsx2": ("pcsx2", ["pcsx2-qt.exe", "pcsx2*.exe"], ["-fullscreen", "-batch", "--", "{rom}"]),
    "duckstation": ("duckstation", ["duckstation-qt*.exe", "duckstation*.exe"], ["-fullscreen", "-batch", "--", "{rom}"]),
    "rpcs3": ("rpcs3", ["rpcs3.exe"], ["--no-gui", "{rom}"]),
    "cemu": ("cemu", ["Cemu.exe", "cemu.exe"], ["-f", "-g", "{rom}"]),
    "dolphin": ("dolphin-emu", ["Dolphin.exe"], ["-b", "-C", "Dolphin.Display.Fullscreen=True", "-e", "{rom}"]),
    "ppsspp": ("ppsspp", ["PPSSPPWindows64.exe", "PPSSPPWindows.exe"], ["--fullscreen", "{rom}"]),
    "xenia-canary": ("xenia-canary", ["xenia_canary.exe", "xenia*.exe"], ["--fullscreen", "{rom}"]),
    "xenia": ("xenia", ["xenia.exe"], ["--fullscreen", "{rom}"]),
    "xemu": ("xemu", ["xemu.exe"], ["-full-screen", "-dvd_path", "{rom}"]),
    "ryujinx": ("ryujinx", ["Ryujinx.exe", "ryujinx*.exe"], ["--fullscreen", "{rom}"]),
    "eden": ("eden", ["eden.exe"], ["-f", "-g", "{rom}"]),
    "citron": ("citron", ["citron.exe"], ["-f", "-g", "{rom}"]),
    "sudachi": ("sudachi", ["sudachi.exe"], ["-f", "-g", "{rom}"]),
    "yuzu": ("yuzu", ["yuzu.exe"], ["-f", "-g", "{rom}"]),
    "azahar": ("azahar", ["azahar.exe", "azahar-qt.exe"], ["-f", "{rom}"]),
    "citra": ("citra", ["citra-qt.exe"], ["-f", "{rom}"]),
    "lime3ds": ("lime3ds", ["lime3ds-gui.exe", "lime-qt.exe"], ["-f", "{rom}"]),
    "flycast": ("flycast", ["flycast.exe"], ["{rom}"]),
    "melonds": ("melonds", ["melonDS.exe"], ["-f", "{rom}"]),
}
NAMES = {"pcsx2": "PCSX2", "duckstation": "DuckStation", "rpcs3": "RPCS3", "cemu": "Cemu", "dolphin": "Dolphin",
         "ppsspp": "PPSSPP", "xenia-canary": "Xenia Canary", "xenia": "Xenia", "xemu": "xemu", "ryujinx": "Ryujinx",
         "eden": "Eden", "citron": "Citron", "sudachi": "Sudachi", "yuzu": "yuzu", "azahar": "Azahar", "citra": "Citra",
         "lime3ds": "Lime3DS", "flycast": "Flycast", "melonds": "melonDS", "libretro": "RetroArch"}


def emu_label(emulator, core=""):
    n = NAMES.get(emulator, emulator)
    return f"{n} · {core}" if emulator == "libretro" and core else n


def _rom_arg(emulator, rom):
    """Ajustes de formato que o RetroBat faz e o emulador sozinho não entende."""
    p = Path(rom)
    if emulator == "rpcs3" and p.suffix.lower() == ".psn":  # arquivo .psn do RetroBat = código do jogo da PSN
        try:
            gid = p.read_text(encoding="utf-8", errors="ignore").strip().splitlines()[0].strip()
            return f"%RPCS3_GAMEID%:{gid}" if gid else str(p)
        except (OSError, IndexError):
            return str(p)
    if emulator == "rpcs3" and p.is_dir():  # pasta .ps3: o EBOOT.BIN do jogo
        for sub in ("PS3_GAME/USRDIR/EBOOT.BIN", "USRDIR/EBOOT.BIN"):
            if (p / sub).is_file():
                return str(p / sub)
    if emulator == "cemu" and p.is_dir():  # pasta .wiiu/.wua extraída: o .rpx em code\
        rpx = sorted(p.glob("code/*.rpx")) or sorted(p.glob("**/code/*.rpx"))
        if rpx:
            return str(rpx[0])
    if p.suffix.lower() == ".m3u" and emulator in ("cemu", "rpcs3", "xemu", "ryujinx", "eden", "citron", "sudachi",
                                                   "yuzu", "xenia", "xenia-canary"):
        from .multidisc import read_m3u
        first = next(iter(read_m3u(rom)), "")
        return first or str(p)
    return str(p)


class EmuConfig:
    def __init__(self, retrobat, settings):
        self.rb, self.settings = retrobat, settings
        self._lock = threading.Lock()
        self._cache = {}  # arquivo -> (mtime, dados)

    # ------------------------------------------------------------------ executável e modo
    def exe(self, emulator):
        """Executável do emulador instalado pelo RetroBat (ou None)."""
        root = self.rb.root()
        spec = DIRECT.get(emulator)
        if not root or not spec:
            return None
        d = Path(root) / "emulators" / spec[0]
        for pat in spec[1]:
            hits = [Path(h) for h in sorted(glob.glob(str(d / pat))) if "updater" not in os.path.basename(h).lower()]
            if hits:
                return hits[0]
        return None

    def direct_supported(self, emulator):
        return emulator in DIRECT

    def system_direct(self, system):
        return bool((self.settings.get("rb_direct") or {}).get(system))

    def set_system_direct(self, system, on):
        d = dict(self.settings.get("rb_direct") or {})
        if on:
            d[system] = True
        else:
            d.pop(system, None)
        self.settings.set("rb_direct", d)

    def wants_direct(self, system, mode=""):
        """mode do jogo: "" = igual ao sistema, "direct", "retrobat"."""
        if mode == "direct":
            return True
        if mode == "retrobat":
            return False
        return self.system_direct(system)

    def direct_command(self, emulator, rom):
        exe = self.exe(emulator)
        if not exe:
            return None
        arg = _rom_arg(emulator, rom)
        return [str(exe)] + [arg if a == "{rom}" else a for a in DIRECT[emulator][2]]

    def open_gui(self, emulator, popen=subprocess.Popen):
        """Abre a janela do emulador (sem jogo) para configurar do jeito dele."""
        exe = self.exe(emulator)
        if not exe:
            if self.rb.menu_dir() and (self.rb.menu_dir() / f"{emulator}.menu").is_file():
                return self.rb.open_emulator(emulator)
            return False, f"O {emu_label(emulator)} ainda não está instalado. Jogue algo dele uma vez pelo RetroBat " \
                          "(ele baixa sozinho) ou instale em Configurações → Emulação → Emuladores."
        log.info("abrindo %s para configurar", exe)
        try:
            popen([str(exe)], cwd=str(exe.parent))
        except OSError as e:
            return False, f"Não consegui abrir o emulador: {e}"
        return True, ""

    # ------------------------------------------------------------------ es_features.cfg + tradução
    def _load(self, path, parse):
        try:
            mt = Path(path).stat().st_mtime
        except OSError:
            return None
        with self._lock:
            hit = self._cache.get(str(path))
            if hit and hit[0] == mt:
                return hit[1]
        data = parse(path)
        with self._lock:
            self._cache[str(path)] = (mt, data)
        return data

    def _features_file(self):
        h = self.rb.home_dir()
        return h / "es_features.cfg" if h else None

    def _po_file(self):
        es = self.rb.es_dir()
        if not es:
            return None
        lang = self.rb.es_get_all().get("Language") or "pt_BR"
        for code in (lang, "pt_BR"):
            p = Path(es) / "es_features.locale" / code / "es-features.po"
            if p.is_file():
                return p
        return None

    @staticmethod
    def _parse_po(path):
        out, cur, key = {}, {}, None
        def flush():
            if cur.get("msgid") and cur.get("msgstr"):
                out[cur["msgid"]] = cur["msgstr"]
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    flush()
                    cur, key = {}, None
                    continue
                m = re.match(r'^(msgctxt|msgid|msgstr)\s+"(.*)"$', line)
                if m:
                    key = m.group(1)
                    cur[key] = _unq(m.group(2))
                elif line.startswith('"') and key:
                    cur[key] = cur.get(key, "") + _unq(line[1:-1])
        flush()
        return out

    @staticmethod
    def _parse_features(path):
        root = ET.parse(path).getroot()
        shared = {}
        sf = root.find("sharedFeatures")
        for f in sf if sf is not None else []:
            if f.tag == "feature" and f.get("value"):
                shared[f.get("value")] = f
        emus = {}
        for e in root.findall("emulator"):
            for name in (n.strip() for n in (e.get("name") or "").split(",")):
                if name:
                    emus[name] = e
        return {"shared": shared, "emus": emus}

    def _tr(self):
        p = self._po_file()
        return (self._load(p, self._parse_po) or {}) if p else {}

    def features(self, system, emulator, core=""):
        """Opções do RetroBat para este sistema + emulador/núcleo, na ordem do RetroBat."""
        f = self._features_file()
        data = self._load(f, self._parse_features) if f and f.is_file() else None
        if not data:
            return []
        e = data["emus"].get(emulator)
        if e is None:
            return []
        nodes = list(_own(e, system))
        cores = e.findall("core")
        if cores:
            c = next((x for x in cores if core in [n.strip() for n in (x.get("name") or "").split(",")]), None)
            if c is None and not core:
                c = cores[0]
            if c is not None:
                nodes += list(_own(c, system))
        tr = self._tr()
        out, seen = [], set()
        for n in nodes:
            if n.tag == "sharedFeature":
                base = data["shared"].get(n.get("value"))
                if base is None:
                    continue
                attrs = dict(base.attrib)
                attrs.update({k: v for k, v in n.attrib.items() if v})
                choices = base.findall("choice")
            else:
                attrs, choices = dict(n.attrib), n.findall("choice")
            opt = self._option(attrs, choices, tr)
            if opt and opt["key"] not in seen:
                seen.add(opt["key"])
                out.append(opt)
        out.sort(key=lambda o: (0 if o["group"] == "GENERAL SETTINGS" else 1, o["order"]))
        return out

    def _option(self, a, choices, tr):
        key, preset = a.get("value", ""), a.get("preset", "")
        if not key or key.startswith("global.") or preset in ("files", "folder", "videomodes", "videofilters"):
            return None
        t = lambda s: tr.get(s, s)
        opt = {"key": key, "label": nice(t(a.get("name", key))), "desc": t(a.get("description", "")),
               "group": a.get("group", ""), "section": nice(t(a.get("submenu") or a.get("group") or "GENERAL SETTINGS")),
               "order": _int(a.get("order"), 999), "type": "choice", "choices": []}
        if opt["group"] == "GENERAL SETTINGS" and not a.get("submenu"):
            opt["section"] = "Principais"
        auto = ["", "Automático"]
        if preset in ("switch", "switchoff"):
            opt.update(type="bool", on="1", off="")
        elif preset == "switchon":
            opt.update(type="bool", on="", off="0", default=True)
        elif preset == "switchauto":
            opt["choices"] = [auto, ["1", "Ligado"], ["0", "Desligado"]]
        elif preset == "sliderauto":
            p = (a.get("preset-parameters") or "").split()
            try:
                lo, hi, st = float(p[0]), float(p[1]), float(p[2])
            except (IndexError, ValueError):
                return None
            unit = " ".join(p[3:])
            vals, v, n = [], lo, 0
            while v <= hi + 1e-9 and n < 400:
                vals.append(round(v, 4))
                v, n = v + st, n + 1
            fmt = lambda x: (str(int(x)) if float(x).is_integer() else f"{x:g}")
            opt["choices"] = [auto] + [[fmt(x), fmt(x) + (f" {unit}" if unit else "")] for x in vals]
        elif preset == "input":
            opt["type"] = "text"
        elif preset in ("shaders", "decorations"):
            opt["choices"] = [auto, ["none", "Nenhum"]] + [[x, x.replace("_", " ")] for x in self._folders(preset)]
        else:
            if not choices:
                return None
            opt["choices"] = [auto] + [[c.get("value", ""), nice(t(c.get("name", c.get("value", ""))), keep=True)]
                                       for c in choices if c.get("value") is not None]
        return opt

    def _folders(self, kind):
        r = self.rb.root()
        if not r:
            return []
        dirs = [Path(r) / "system" / "shaders" / "configs"] if kind == "shaders" else \
               [Path(r) / "system" / "decorations", Path(r) / "decorations"]
        out = set()
        for d in dirs:
            try:
                out.update(x.name for x in d.iterdir() if x.is_dir())
            except OSError:
                pass
        return sorted(out)

    # ------------------------------------------------------------------ valores (es_settings.cfg)
    @staticmethod
    def prefix(system, rom=None):
        return f'{system}["{os.path.basename(str(rom))}"].' if rom else f"{system}."

    def values(self, system, rom=None):
        """{opção: valor} gravados no escopo (sistema ou jogo) e, para o jogo, os do sistema (herdados)."""
        cfg = self.rb.es_get_all()
        sp, gp = self.prefix(system), self.prefix(system, rom) if rom else None
        sysv = {k[len(sp):]: v for k, v in cfg.items() if k.startswith(sp)}
        gamev = {k[len(gp):]: v for k, v in cfg.items() if gp and k.startswith(gp)}
        return sysv, gamev

    def set_value(self, system, rom, key, value):
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", key or ""):
            return False
        v = None if value in (None, "", False) else ("1" if value is True else str(value))
        return self.rb.es_set({self.prefix(system, rom) + key: v})

    def reset(self, system, rom=None, keys=None):
        cfg = self.rb.es_get_all()
        p = self.prefix(system, rom)
        skip = {"emulator", "core"}
        drop = {k: None for k in cfg if k.startswith(p) and k[len(p):] not in skip and (keys is None or k[len(p):] in keys)}
        return self.rb.es_set(drop) if drop else True


def _own(node, system):
    """Opções diretas do nó + as do bloco <system name="..."> deste sistema."""
    for x in node:
        if x.tag in ("feature", "sharedFeature"):
            yield x
        elif x.tag == "system" and system in [s.strip() for s in (x.get("name") or "").split(",")]:
            for y in x:
                if y.tag in ("feature", "sharedFeature"):
                    yield y


def _unq(s):
    return s.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


def _int(s, d):
    try:
        return int(s)
    except (TypeError, ValueError):
        return d


ACRONYMS = {"BIOS", "FMV", "VU", "VU1", "MTVU", "CPU", "GPU", "FPS", "HD", "HDR", "RA", "PS1", "PS2", "PS3", "PSX", "PSP",
            "UI", "MSAA", "FXAA", "SSAA", "TAA", "VRAM", "RAM", "ROM", "EE", "IOP", "GS", "NTSC", "PAL", "CRT", "LCD",
            "USB", "SDL", "DSU", "OSD", "ID", "CD", "DVD", "API", "VSYNC", "V-SYNC", "AI", "HLE", "LLE", "JIT", "EFB",
            "XFB", "OPENGL", "D3D", "D3D11", "D3D12", "DX11", "DX12", "LLVM", "PPU", "SPU", "RSX", "ZCULL", "HUD", "VR",
            "FSR", "CAS", "SMAA", "TV", "MIDI", "MAME", "SNES", "NES", "N64", "GBA", "DS", "3DS", "WII", "XBOX", "GC",
            "CHD", "ISO", "BOOT", "IPL", "FPU", "MMU", "GPI", "ANGLE", "4K", "8K", "2X", "3X", "4X", "WIIMOTE", "SD"}


def nice(s, keep=False):
    """'RESOLUÇÃO INTERNA' -> 'Resolução interna' (as opções do RetroBat vêm em MAIÚSCULAS). keep=True deixa como
    está quem já não for tudo maiúsculo (valores como '2X (1280x960)')."""
    s = (s or "").strip()
    letters = [c for c in s if c.isalpha()]
    if not letters or not all(c.isupper() for c in letters):
        return s
    words = []
    for i, w in enumerate(re.split(r"(\s+)", s)):
        core = re.sub(r"[^\w\-]", "", w).upper()
        if core in ACRONYMS or re.search(r"\d", w):
            words.append(w)
        else:
            words.append(w.lower())
    out = "".join(words)
    out = out[:1].upper() + out[1:]
    for low, proper in PROPER.items():
        out = re.sub(rf"\b{low}\b", proper, out, flags=re.I)
    return out


PROPER = {"retroachievements": "RetroAchievements", "retrobat": "RetroBat", "reshade": "ReShade", "retroarch": "RetroArch",
          "pcsx2": "PCSX2", "rpcs3": "RPCS3", "cemu": "Cemu", "dolphin": "Dolphin", "duckstation": "DuckStation",
          "vulkan": "Vulkan", "directx": "DirectX", "opengl": "OpenGL", "windows": "Windows", "xinput": "XInput",
          "dualshock": "DualShock", "dualsense": "DualSense", "wiimote": "Wiimote"}
