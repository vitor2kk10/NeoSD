"""RetroBat como motor de emulação do NeoSD.

O RetroBat fica instalado sem modificação; a interface dele (EmulationStation) nem é aberta. O NeoSD:
  - lê os sistemas de emulationstation\\.emulationstation\\es_systems.cfg (+ es_systems_*.cfg);
  - encontra as ROMs em roms\\<sistema> e junta os dados das gamelist.xml (nome, sinopse, imagens);
  - inicia os jogos chamando emulationstation\\emulatorLauncher.exe exatamente como a interface faz:

      emulatorLauncher.exe [-p1index 0 -p1guid … -p1path … -p1name … -p1nbbuttons … -p1nbhats … -p1nbaxes …]
                           -system snes -emulator libretro -core snes9x -rom "C:\\…\\jogo.sfc"

    (comando confirmado no emulatorLauncher.log do RetroBat 8.4.0)
  - lê e grava as opções em es_settings.cfg (o emulatorLauncher carrega esse arquivo a cada jogo).

Os controles são listados com o SDL2.dll do próprio RetroBat (o mesmo que a interface dele usa), para que
os argumentos -pN* batam com os perfis do es_input.cfg e a configuração automática de controle funcione.
"""
import ctypes
import logging
import os
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

log = logging.getLogger("neosd.retrobat")

# "sistemas" internos do RetroBat que não são jogos: menu dos emuladores, visualizadores de imagem/vídeo, MESS
NOT_GAME_SYSTEMS = {"retrobat", "library", "screenshots", "mess", "imageviewer"}
MEDIA_DIRS = {"images", "videos", "manuals", "media", "downloaded_images", "downloaded_videos", "snap", "marquees"}
SKIP_FILES = {"gamelist.xml", "_info.txt", "systeminfo.txt"}


def _drives():
    import string
    return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")] if os.name == "nt" else []


class RetroBatService:
    def __init__(self, settings, env=None):
        self.settings = settings
        self.env = os.environ if env is None else env
        self._systems = None
        self._systems_mtime = None
        self.lock = threading.RLock()
        self.on_launch = lambda system, rom, proc: None

    # ------------------------------------------------------------------ onde está
    @staticmethod
    def valid(root):
        return bool(root) and (Path(root) / "emulationstation" / "emulatorLauncher.exe").is_file()

    def candidates(self):
        home = Path(self.env.get("USERPROFILE") or Path.home())
        out = self._registered()
        out += [home / "Desktop" / "RetroBat", home / "RetroBat", home / "Documents" / "RetroBat",
                home / "OneDrive" / "Desktop" / "RetroBat", home / "OneDrive" / "Área de Trabalho" / "RetroBat"]
        out += [Path(d) / "RetroBat" for d in _drives()]
        out += [Path(d) / "Games" / "RetroBat" for d in _drives()]
        return out

    @staticmethod
    def _registered():
        """Pasta gravada pelo instalador oficial na lista de programas instalados do Windows (qualquer pasta)."""
        try:
            import winreg
        except ImportError:
            return []
        found, key = [], r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        views = [(winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                 (winreg.HKEY_CURRENT_USER, 0)]
        for hive, view in views:
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sub:
                                if "retrobat" not in str(winreg.QueryValueEx(sub, "DisplayName")[0]).lower():
                                    continue
                                for val in ("InstallLocation", "UninstallString", "DisplayIcon"):
                                    try:
                                        v = str(winreg.QueryValueEx(sub, val)[0]).strip().strip('"').split('"')[0]
                                    except OSError:
                                        continue
                                    if v:
                                        p = Path(v)
                                        found.append(p if val == "InstallLocation" else p.parent)
                        except OSError:
                            continue
            except OSError:
                continue
        return found

    def forget(self):
        """Esquece a pasta salva se ela sumiu e limpa o cache (depois de instalar ou desinstalar o RetroBat)."""
        if not self.valid(self.settings.get("retrobat_root", "")):
            self.settings.set("retrobat_root", "")
        self._systems = None

    def root(self):
        saved = self.settings.get("retrobat_root", "")
        if self.valid(saved):
            return Path(saved)
        for c in self.candidates():
            if self.valid(c):
                self.settings.set("retrobat_root", str(c))
                return c
        return None

    def set_root(self, path):
        if not self.valid(path):
            return False, "Essa pasta não parece ser do RetroBat (falta emulationstation\\emulatorLauncher.exe)."
        self.settings.set("retrobat_root", str(Path(path)))
        self._systems = None
        return True, "RetroBat encontrado."

    def es_dir(self):
        r = self.root()
        return r / "emulationstation" if r else None

    def home_dir(self):
        e = self.es_dir()
        return e / ".emulationstation" if e else None

    def version(self):
        try:
            return (self.es_dir() / "version.info").read_text(encoding="utf-8", errors="ignore").strip()
        except (OSError, TypeError):
            return ""

    # ------------------------------------------------------------------ sistemas
    def _resolve(self, p):
        """'~\\..\\roms\\snes' -> <RetroBat>\\roms\\snes  ('~' = pasta emulationstation)."""
        p = (p or "").strip()
        if p.startswith("~"):
            p = str(self.es_dir()) + p[1:]
        return Path(os.path.normpath(p.replace("/", os.sep).replace("\\", os.sep)))

    def systems(self):
        """Lista de sistemas (cache até o es_systems.cfg mudar)."""
        home = self.home_dir()
        if not home:
            return []
        files = sorted(home.glob("es_systems*.cfg"))
        mtime = tuple((f.name, f.stat().st_mtime) for f in files)
        with self.lock:
            if self._systems is not None and self._systems_mtime == mtime:
                return self._systems
            out, seen = [], set()
            for f in files:
                try:
                    root = ET.parse(f).getroot()
                except (ET.ParseError, OSError):
                    log.exception("não consegui ler %s", f)
                    continue
                for s in root.findall("system"):
                    name = (s.findtext("name") or "").strip()
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    emus = []
                    for e in s.findall("emulators/emulator"):
                        emus.append({"name": e.get("name", ""),
                                     "cores": [c.text.strip() for c in e.findall("cores/core") if c.text and c.text.strip()]})
                    out.append({
                        "name": name,
                        "fullname": (s.findtext("fullname") or name).strip(),
                        "manufacturer": (s.findtext("manufacturer") or "").strip(),
                        "release": (s.findtext("release") or "").strip(),
                        "hardware": (s.findtext("hardware") or "").strip(),
                        "path": str(self._resolve(s.findtext("path"))),
                        "extensions": [x.lower() for x in (s.findtext("extension") or "").split() if x.startswith(".")],
                        "emulators": emus,
                        "platform": (s.findtext("platform") or "").strip(),
                    })
            self._systems, self._systems_mtime = out, mtime
            return out

    def system(self, name):
        return next((s for s in self.systems() if s["name"] == name), None)

    @staticmethod
    def is_game_system(s):
        return (s["name"] not in NOT_GAME_SYSTEMS and s.get("platform") != "imageviewer"
                and s.get("hardware") not in ("system",) and bool(s.get("extensions")))

    def game_systems(self):
        return [s for s in self.systems() if self.is_game_system(s)]

    # ------------------------------------------------------------------ jogos
    @staticmethod
    def _gamelist(folder):
        """{caminho absoluto normalizado: dados} a partir de <pasta>/gamelist.xml."""
        gl = Path(folder) / "gamelist.xml"
        data = {}
        if not gl.is_file():
            return data
        try:
            root = ET.parse(gl).getroot()
        except (ET.ParseError, OSError):
            log.warning("gamelist inválida: %s", gl)
            return data
        for g in root.findall("game"):
            rel = (g.findtext("path") or "").strip()
            if not rel:
                continue
            info = {c.tag: (c.text or "").strip() for c in g}
            for k in ("image", "thumbnail", "marquee", "fanart", "video", "titleshot", "boxart", "wheel"):
                if info.get(k):
                    info[k] = str((Path(folder) / info[k]).resolve()) if not os.path.isabs(info[k]) else info[k]
            if info.get("hidden") == "true":
                continue
            full = rel if os.path.isabs(rel) else str((Path(folder) / rel).resolve())
            data[os.path.normcase(full)] = info
        return data

    def _scan_folder(self, sysinfo):
        """ROMs da pasta do sistema: arquivos com extensão aceita (até 2 níveis), pastas com extensão
        (ex.: jogo.ps3) e .m3u escondendo os discos que ele lista, como a interface do RetroBat faz."""
        root, exts = Path(sysinfo["path"]), set(sysinfo["extensions"])
        found, hidden = [], set()
        if not root.is_dir():
            return found

        def walk(folder, depth):
            try:
                entries = sorted(os.scandir(folder), key=lambda e: e.name.casefold())
            except OSError:
                return
            for e in entries:
                if e.name.startswith(".") or e.name.lower() in SKIP_FILES:
                    continue
                ext = os.path.splitext(e.name)[1].lower()
                if e.is_dir():
                    if ext and ext in exts:  # pasta-jogo (ex.: Jogo.ps3, Jogo.pc)
                        found.append(e.path)
                    elif depth < 2 and e.name.lower() not in MEDIA_DIRS:
                        walk(e.path, depth + 1)
                elif ext in exts:
                    found.append(e.path)
                    if ext == ".m3u":
                        try:
                            for line in Path(e.path).read_text(encoding="utf-8", errors="ignore").splitlines():
                                line = line.strip()
                                if line and not line.startswith("#"):
                                    hidden.add(os.path.normcase(str((Path(e.path).parent / line).resolve())))
                        except OSError:
                            pass

        walk(root, 0)
        found = [p for p in found if os.path.normcase(str(Path(p).resolve())) not in hidden]
        if ".m3u" in exts and self.settings.get("rb_auto_m3u", True):
            found = self._multidisc(found)
        return found

    @staticmethod
    def _multidisc(found):
        """'Jogo (Disc 1).cue' + '(Disc 2).cue' sem .m3u: cria 'Jogo.m3u' na pasta e mostra só ele (um jogo só,
        com troca de disco pelo emulador). Se a pasta não deixar gravar, os discos continuam separados."""
        from .multidisc import groups, write_m3u
        files = [p for p in found if os.path.isfile(p)]
        out, drop = list(found), set()
        for folder, base, items in groups(files):
            m3u = write_m3u(folder, base, items)
            if not m3u:
                continue
            drop.update(os.path.normcase(p) for _, p in items)
            if os.path.normcase(m3u) not in {os.path.normcase(p) for p in out}:
                out.append(m3u)
        return sorted((p for p in out if os.path.normcase(p) not in drop), key=lambda p: os.path.basename(p).casefold())

    def games(self, system_name):
        s = self.system(system_name)
        if not s:
            return []
        meta = self._gamelist(s["path"])
        out = []
        for p in self._scan_folder(s):
            info = meta.get(os.path.normcase(str(Path(p).resolve())), {})
            if not info and p.lower().endswith(".m3u"):  # lista de discos: usa os dados do disco 1 da gamelist
                from .multidisc import read_m3u, split_disc
                first = next(iter(read_m3u(p)), "")
                info = dict(meta.get(os.path.normcase(str(Path(first).resolve())), {})) if first else {}
                if info.get("name"):
                    info["name"] = split_disc(info["name"])[0]
            stem = Path(p).stem if Path(p).is_file() or Path(p).suffix else Path(p).name
            out.append({"system": s["name"], "system_name": s["fullname"], "rom": p,
                        "name": info.get("name") or stem, "info": info})
        return out

    def summary(self):
        """Sistemas que têm jogos: [{name, fullname, count}]."""
        res = []
        for s in self.game_systems():
            try:
                n = len(self._scan_folder(s))
            except Exception:
                n = 0
            if n:
                res.append({"name": s["name"], "fullname": s["fullname"], "count": n})
        return res

    def systems_for_file(self, path):
        """Sistemas que aceitam a extensão do arquivo (para 'Adicionar ROM')."""
        ext = os.path.splitext(str(path))[1].lower()
        return [{"name": s["name"], "fullname": s["fullname"], "manufacturer": s["manufacturer"]}
                for s in self.game_systems() if ext and ext in s["extensions"]]

    # ------------------------------------------------------------------ emuladores (menu do RetroBat)
    def menu_dir(self):
        r = self.root()
        return r / "system" / "es_menu" if r else None

    def emulators(self, thumbs=None):
        """Emuladores do menu do RetroBat (system\\es_menu\\*.menu): nome, descrição, logo e se já está instalado.
        Abrir um deles chama 'emulatorLauncher -system retrobat -rom <arquivo.menu>': o RetroBat baixa e instala
        o emulador se ainda não tiver, e abre a tela dele."""
        d, r = self.menu_dir(), self.root()
        if not d or not d.is_dir():
            return []
        meta = self._gamelist(d)
        out = []
        for f in sorted(d.glob("*.menu"), key=lambda p: p.stem.lower()):
            info = meta.get(os.path.normcase(str(f.resolve())), {})
            try:
                first = f.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            except (OSError, IndexError):
                first = ""
            exe = (r / "emulators" / first.replace("\\", "/").lstrip("/")) if first and not first.startswith("-") else None
            installed = bool(exe and exe.is_file())
            logo = info.get("image") or info.get("marquee") or ""
            out.append({"id": f.stem, "name": info.get("name") or f.stem, "desc": info.get("desc", ""),
                        "developer": info.get("developer", ""), "installed": installed,
                        "logo": thumbs(logo) if thumbs and logo else ""})
        return out

    def open_emulator(self, emu_id, popen=subprocess.Popen):
        d = self.menu_dir()
        f = d / f"{os.path.basename(emu_id)}.menu" if d else None
        if not f or not f.is_file():
            return False, "Emulador não encontrado no menu do RetroBat."
        cmd = [str(self.es_dir() / "emulatorLauncher.exe"), "-system", "retrobat", "-rom", str(f)]
        log.info("RetroBat (emulador): %s", subprocess.list2cmdline(cmd))
        try:
            popen(cmd, cwd=str(self.es_dir()))
        except OSError as e:
            return False, f"Não consegui abrir o emulatorLauncher: {e}"
        return True, ""

    # ------------------------------------------------------------------ es_settings.cfg
    def _settings_path(self):
        h = self.home_dir()
        return h / "es_settings.cfg" if h else None

    def es_get_all(self):
        p = self._settings_path()
        out = {}
        if not p or not p.is_file():
            return out
        try:
            for el in ET.parse(p).getroot():
                if el.get("name") is not None:
                    out[el.get("name")] = el.get("value", "")
        except (ET.ParseError, OSError):
            log.exception("es_settings.cfg ilegível")
        return out

    def es_set(self, changes):
        """Grava {chave: valor} em es_settings.cfg (valor '' ou None remove a chave = volta ao automático).
        Faz backup (.neosd.bak) antes da primeira alteração."""
        p = self._settings_path()
        if not p:
            return False
        with self.lock:
            try:
                tree = ET.parse(p) if p.is_file() else ET.ElementTree(ET.Element("config"))
            except ET.ParseError:
                return False
            root = tree.getroot()
            bak = p.with_suffix(".cfg.neosd.bak")
            if p.is_file() and not bak.exists():
                bak.write_bytes(p.read_bytes())
            index = {el.get("name"): el for el in root if el.get("name") is not None}
            for k, v in changes.items():
                el = index.get(k)
                if v in (None, ""):
                    if el is not None:
                        root.remove(el)
                    continue
                if isinstance(v, bool):
                    tag, val = "bool", "true" if v else "false"
                else:
                    tag, val = ("string", str(v)) if el is None or el.tag != "bool" else ("bool", str(v).lower())
                if el is None:
                    el = ET.SubElement(root, tag, {"name": k, "value": val})
                else:
                    el.tag = tag
                    el.set("value", val)
            # mesmo formato do arquivo original (uma linha por chave, tab, ordem alfabética por tipo)
            items = sorted(root, key=lambda e: ({"bool": 0, "int": 1, "string": 2}.get(e.tag, 3), e.get("name", "")))
            root[:] = items
            ET.indent(tree, space="\t")
            tmp = p.with_suffix(".cfg.tmp")
            body = ET.tostring(root, encoding="unicode")
            tmp.write_text('<?xml version="1.0"?>\n' + body + "\n", encoding="utf-8")
            os.replace(tmp, p)
            return True

    # ------------------------------------------------------------------ emulador escolhido
    def pick_emulator(self, system_name, rom=None, override=None):
        """Emulador/core na mesma ordem de prioridade da interface do RetroBat:
        jogo (NeoSD ou es_settings sistema["arquivo"]) -> sistema (es_settings) -> padrão (primeiro do es_systems)."""
        s = self.system(system_name) or {"emulators": []}
        cfg = self.es_get_all()
        emu = core = ""
        if override and override.get("emulator"):
            emu, core = override.get("emulator", ""), override.get("core", "")
        else:
            if rom:
                key = f'{system_name}["{os.path.basename(str(rom))}"]'
                emu, core = cfg.get(key + ".emulator", ""), cfg.get(key + ".core", "")
            if not emu:
                emu, core = cfg.get(f"{system_name}.emulator", ""), cfg.get(f"{system_name}.core", "")
        if not emu and s["emulators"]:
            emu = s["emulators"][0]["name"]
        if emu and not core:
            e = next((x for x in s["emulators"] if x["name"] == emu), None)
            core = e["cores"][0] if e and e["cores"] else ""
        return emu, core

    # ------------------------------------------------------------------ controles (SDL2 do RetroBat)
    def controllers(self):
        """[{index, guid, path, name, buttons, hats, axes}] como a interface do RetroBat informa ao emulatorLauncher."""
        es = self.es_dir()
        dll = es / "SDL2.dll" if es else None
        if os.name != "nt" or not dll or not dll.is_file():
            return []
        try:
            return _sdl_list(str(dll))
        except Exception:
            log.exception("falha ao listar controles com o SDL2 do RetroBat")
            return []

    @staticmethod
    def controller_args(pads):
        args = []
        for n, p in enumerate(pads[:8], 1):
            args += [f"-p{n}index", str(p["index"]), f"-p{n}guid", p["guid"]]
            if p.get("path"):
                args += [f"-p{n}path", p["path"]]
            args += [f"-p{n}name", p["name"], f"-p{n}nbbuttons", str(p["buttons"]),
                     f"-p{n}nbhats", str(p["hats"]), f"-p{n}nbaxes", str(p["axes"])]
        return args

    # ------------------------------------------------------------------ iniciar
    def command(self, system_name, rom, override=None, pads=None):
        es = self.es_dir()
        emu, core = self.pick_emulator(system_name, rom, override)
        pads = self.controllers() if pads is None else pads
        cmd = [str(es / "emulatorLauncher.exe")] + self.controller_args(pads) + ["-system", system_name]
        if emu:
            cmd += ["-emulator", emu]
        if core:
            cmd += ["-core", core]
        return cmd + ["-rom", str(rom)]

    def launch(self, system_name, rom, override=None, popen=subprocess.Popen, mode=""):
        """mode: "" = igual ao sistema, "direct" = direto no emulador (configurações dele), "retrobat"."""
        if not self.root():
            return False, "RetroBat não encontrado. Indique a pasta em Configurações → Bibliotecas."
        if not os.path.exists(rom):
            return False, "ROM não encontrada."
        if not self.system(system_name):
            return False, f"O RetroBat não tem o sistema “{system_name}”."
        cmd, cwd = None, str(self.es_dir())
        ec = getattr(self, "emucfg", None)
        if ec and ec.wants_direct(system_name, mode):
            emu, _ = self.pick_emulator(system_name, rom, override)
            cmd = ec.direct_command(emu, rom)
            if cmd:  # sem o emulatorLauncher: o emulador usa as configurações que o usuário fez nele
                cwd = str(Path(cmd[0]).parent)
                log.info("direto no emulador: %s", subprocess.list2cmdline(cmd))
            else:
                log.info("modo direto pedido, mas %s não abre direto (ou não está instalado): indo pelo RetroBat", emu)
        if not cmd:
            cmd = self.command(system_name, rom, override)
            log.info("RetroBat (%d controle(s)): %s", sum(1 for a in cmd if a.endswith("index")), subprocess.list2cmdline(cmd))
        try:
            proc = popen(cmd, cwd=cwd)
        except OSError as e:
            return False, f"Não consegui abrir o emulatorLauncher: {e}"
        try:
            self.on_launch(system_name, rom, proc)  # ex.: acompanhar o RetroAchievements enquanto o jogo roda
        except Exception:
            log.exception("depois de abrir o jogo")
        return True, ""

    # ------------------------------------------------------------------ opções para a interface
    def game_emulators(self, system_name):
        s = self.system(system_name)
        if not s:
            return []
        out = [["", "Igual ao sistema"]]
        for e in s["emulators"]:
            out += [[f'{e["name"]}/{c}', f'{e["name"]} · {c}'] for c in e["cores"]] if e["cores"] else [[e["name"], e["name"]]]
        return out

    def options(self):
        """Opções principais (globais e por sistema com jogos) no formato da tela de configurações."""
        r = self.root()
        cfg = self.es_get_all() if r else {}
        def folders(p):
            try:
                return sorted(d.name for d in Path(p).iterdir() if d.is_dir())
            except OSError:
                return []
        shaders = folders(r / "system" / "shaders" / "configs") if r else []
        bezels = sorted(set(folders(r / "system" / "decorations") + folders(r / "decorations"))) if r else []
        glob_opts = [
            {"key": "global.shaderset", "label": "Shader (efeito de tela)", "type": "choice",
             "choices": [["", "Automático"], ["none", "Nenhum"]] + [[s, s] for s in shaders]},
            {"key": "global.bezel", "label": "Molduras (decorações)", "type": "choice",
             "choices": [["", "Automático"], ["none", "Nenhuma"]] + [[b, b.replace("_", " ")] for b in bezels]},
            {"key": "global.ratio", "label": "Proporção da imagem", "type": "choice",
             "choices": [["", "Automático"], ["4/3", "4:3"], ["16/9", "16:9"], ["16/10", "16:10"], ["core", "Do núcleo"],
                         ["full", "Tela cheia (esticar)"]]},
            {"key": "global.smooth", "label": "Suavizar imagem (filtro bilinear)", "type": "bool"},
            {"key": "global.integerscale", "label": "Escala inteira (pixels nítidos)", "type": "bool"},
            {"key": "global.rewind", "label": "Voltar no tempo (rewind)", "type": "bool"},
            {"key": "global.autosave", "label": "Salvar e carregar automaticamente", "type": "bool"},
        ]
        for o in glob_opts:
            o["value"] = cfg.get(o["key"], "")
        systems = []
        for sm in self.summary() if r else []:
            s = self.system(sm["name"])
            choices = [["", "Automático (padrão do RetroBat)"]]
            for e in s["emulators"]:
                if e["cores"]:
                    choices += [[f'{e["name"]}/{c}', f'{e["name"]} · {c}'] for c in e["cores"]]
                else:
                    choices.append([e["name"], e["name"]])
            emu, core = cfg.get(f'{s["name"]}.emulator', ""), cfg.get(f'{s["name"]}.core', "")
            systems.append({"name": s["name"], "fullname": s["fullname"], "count": sm["count"],
                            "value": f"{emu}/{core}" if emu and core else emu, "choices": choices})
        return {"global": glob_opts, "systems": systems}

    def set_option(self, key, value):
        if key == "neosd.in_games":
            self.settings.set("rb_in_games", value in (True, "true"))
            return True
        if key.endswith("@emu"):  # sistema: "libretro/snes9x" ou "duckstation"
            sysname = key[:-4]
            emu, _, core = (value or "").partition("/")
            return self.es_set({f"{sysname}.emulator": emu or None, f"{sysname}.core": core or None})
        if not key.startswith("global."):
            return False
        if value in (True, False, "true", "false"):
            value = value in (True, "true")
        return self.es_set({key: value})

    def status(self):
        r = self.root()
        if not r:
            return {"found": False, "root": "", "version": "", "systems": [], "in_games": False}
        return {"found": True, "root": str(r), "version": self.version(), "systems": self.summary(),
                "in_games": bool(self.settings.get("rb_in_games", False))}


# ---------------------------------------------------------------------- SDL2 via ctypes
class _GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]


def _sdl_list(dll_path):
    """Lista joysticks com o SDL2.dll indicado. GUID no formato do es_input.cfg (CRC zerado, minúsculas)."""
    os.add_dll_directory(os.path.dirname(dll_path)) if hasattr(os, "add_dll_directory") else None
    sdl = ctypes.CDLL(dll_path)
    sdl.SDL_Init.argtypes = [ctypes.c_uint32]
    sdl.SDL_NumJoysticks.restype = ctypes.c_int
    sdl.SDL_JoystickNameForIndex.restype = ctypes.c_char_p
    sdl.SDL_JoystickNameForIndex.argtypes = [ctypes.c_int]
    sdl.SDL_JoystickGetDeviceGUID.restype = _GUID
    sdl.SDL_JoystickGetDeviceGUID.argtypes = [ctypes.c_int]
    sdl.SDL_JoystickGetGUIDString.argtypes = [_GUID, ctypes.c_char_p, ctypes.c_int]
    sdl.SDL_JoystickOpen.restype = ctypes.c_void_p
    sdl.SDL_JoystickOpen.argtypes = [ctypes.c_int]
    for fn in ("SDL_JoystickNumButtons", "SDL_JoystickNumHats", "SDL_JoystickNumAxes"):
        getattr(sdl, fn).argtypes = [ctypes.c_void_p]
        getattr(sdl, fn).restype = ctypes.c_int
    sdl.SDL_JoystickClose.argtypes = [ctypes.c_void_p]
    has_path = hasattr(sdl, "SDL_JoystickPathForIndex")
    if has_path:
        sdl.SDL_JoystickPathForIndex.restype = ctypes.c_char_p
        sdl.SDL_JoystickPathForIndex.argtypes = [ctypes.c_int]
    if hasattr(sdl, "SDL_SetHint"):
        sdl.SDL_SetHint.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        sdl.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")  # o NeoSD não tem janela SDL em foco
    # JOYSTICK | GAMECONTROLLER | EVENTS, como um frontend SDL faz
    if sdl.SDL_Init(0x00000200 | 0x00002000 | 0x00004000) != 0 and sdl.SDL_Init(0x00000200) != 0:
        return []
    # alguns drivers (RawInput/HIDAPI/XInput) só aparecem depois de bombear eventos: espera a contagem estabilizar
    pump = getattr(sdl, "SDL_PumpEvents", None)
    upd = getattr(sdl, "SDL_JoystickUpdate", None)
    last, stable, t0 = -1, 0, time.monotonic()
    while time.monotonic() - t0 < 1.5:
        if pump:
            pump()
        if upd:
            upd()
        n = sdl.SDL_NumJoysticks()
        stable = stable + 1 if n == last else 0
        last = n
        if stable >= 4 and (n > 0 or time.monotonic() - t0 > 0.6):
            break
        time.sleep(0.05)
    pads = []
    try:
        for i in range(sdl.SDL_NumJoysticks()):
            buf = ctypes.create_string_buffer(33)
            sdl.SDL_JoystickGetGUIDString(sdl.SDL_JoystickGetDeviceGUID(i), buf, 33)
            guid = buf.value.decode("ascii", "ignore").lower()
            if len(guid) == 32:  # bytes 2-3 = CRC do nome; o RetroBat usa zerado (igual ao es_input.cfg)
                guid = guid[:4] + "0000" + guid[8:]
            name = (sdl.SDL_JoystickNameForIndex(i) or b"").decode("utf-8", "ignore")
            path = (sdl.SDL_JoystickPathForIndex(i) or b"").decode("utf-8", "ignore") if has_path else ""
            j = sdl.SDL_JoystickOpen(i)
            b = h = a = 0
            if j:
                b, h, a = sdl.SDL_JoystickNumButtons(j), sdl.SDL_JoystickNumHats(j), sdl.SDL_JoystickNumAxes(j)
                sdl.SDL_JoystickClose(j)
            pads.append({"index": i, "guid": guid, "path": path, "name": name, "buttons": b, "hats": h, "axes": a})
    finally:
        sdl.SDL_Quit()
    return pads
