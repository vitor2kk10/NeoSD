"""NEO SD — launcher de jogos com cartucho SD. Janela = pywebview (interface em ui/index.html)."""
import argparse
import ctypes
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import webview

from core.controller import watch_combo
from core.games import GameService, clean_rom_name
from core import instance
from core.anyfse import AnyFSEService
from core.apps import AppsService
from core.retrobat import RetroBatService
from core.rbsetup import RetroBatSetup
from core.achievements import SteamAchievements
from core.overlay import OverlayService
from core.sfx import SfxService
from core.updater import Updater
from core.discs import DiscService, DiscMonitor, PLAYABLE
from core.retroachievements import RetroAchievementsService, CONSOLES as RA_CONSOLES
from core.network import NetworkService
from core.backdrop import BackdropService
from core.mediaserver import MediaServer
from core.libraries import LibraryService
from core.settings import AppSettings
from core.system import SystemService
from core.media import load_art
from core.scraper import ScraperError, ScraperService
from core.sd import SDMonitor, disk_info, find_cartridge, read_cartridge
from core.store import BASE

HERE = Path(__file__).resolve().parent
UI_FILE = Path(getattr(sys, "_MEIPASS", HERE)) / "ui" / "index.html"  # _MEIPASS = pasta de dados do PyInstaller
DATA, LIBRARY = BASE, BASE / "library"  # dados graváveis (no .exe: %LOCALAPPDATA%\\NeoSD)


class Api:
    """Métodos públicos = chamáveis pelo JavaScript (window.pywebview.api.*)."""

    def __init__(self, service, scraper, libs=None, app=None, apps=None, system=None, backdrop=None):
        self._svc = service      # '_' = escondido do JS
        self._scraper = scraper
        self._libs, self._app, self._apps, self._system = libs, app, apps, system
        self._bd = backdrop
        self._emit = lambda fn, payload: None  # preenchido no main()
        self._window = None
        self._fullscreen = False

    def get_games(self):
        return self._svc.list_ui()

    def launch_game(self, game_id):
        ok, msg = self._svc.launch(game_id)
        return {"ok": ok, "msg": msg}

    # ---- adicionar / editar jogo ----
    def get_game_form(self, game_id=None):
        return self._svc.get_form(game_id)

    def save_game(self, form, original_id=None):
        ok, msg, game_id = self._svc.save_game(form, original_id)
        return {"ok": ok, "msg": msg, "id": game_id}

    def delete_game(self, game_id):
        return {"ok": self._svc.delete_game(game_id)}

    def _dialog(self, kind, types=(), multiple=False):
        FD = getattr(webview, "FileDialog", None)  # pywebview 5+ ; versões antigas usam as constantes
        if kind == "folder":
            return self._window.create_file_dialog(FD.FOLDER if FD else webview.FOLDER_DIALOG)
        return self._window.create_file_dialog(FD.OPEN if FD else webview.OPEN_DIALOG,
                                               allow_multiple=multiple, file_types=types)

    def pick_file(self, kind):
        """Seletor de arquivo nativo. kind: exe | rom | cover | icon | emu_exe | core."""
        types = {"exe": ("Executáveis (*.exe;*.bat)", "Todos os arquivos (*.*)"),
                 "emu_exe": ("Executáveis (*.exe)", "Todos os arquivos (*.*)"),
                 "core": ("Cores libretro (*.dll)", "Todos os arquivos (*.*)"),
                 "rom": ("Todos os arquivos (*.*)",),
                 "cover": ("Imagens (*.jpg;*.jpeg;*.png)",),
                 "icon": ("Imagens (*.jpg;*.jpeg;*.png;*.ico;*.bmp)",)}[kind]
        result = self._dialog("open", types)
        if not result:
            return None
        path = result[0]
        preview = load_art(path, kind)[0] if kind in ("cover", "icon") else None
        return {"path": path, "preview": preview}

    # ---- ajustes rápidos de um jogo (modo console) ----
    def game_set_art(self, game_id, role):
        types = ("Imagens (*.jpg;*.jpeg;*.png;*.webp;*.bmp)",) if role != "icon" else ("Imagens (*.png;*.jpg;*.jpeg;*.ico;*.bmp)",)
        result = self._dialog("open", types)
        if not result:
            return {"ok": False, "msg": ""}
        ok, msg = self._svc.set_art(game_id, role, result[0])
        return {"ok": ok, "msg": msg}

    def game_set_target(self, game_id):
        g = self._svc.get_form(game_id)["form"]
        kind = "rom" if g.get("launch_type") in ("emulator", "retrobat") else "exe"
        types = ("Todos os arquivos (*.*)",) if kind == "rom" else ("Executáveis (*.exe;*.bat;*.lnk)", "Todos os arquivos (*.*)")
        result = self._dialog("open", types)
        if not result:
            return {"ok": False, "msg": ""}
        ok, msg = self._svc.set_target(game_id, result[0])
        return {"ok": ok, "msg": msg}

    def game_rename(self, game_id, name):
        data = self._svc.get_form(game_id)["form"]
        data["name"] = name
        ok, msg, new_id = self._svc.save_game(data, game_id)
        return {"ok": ok, "msg": msg or "Nome atualizado.", "id": new_id}

    def game_pick_exe(self):
        """Adicionar jogo do PC: escolhe o .exe e sugere um nome."""
        result = self._dialog("open", ("Executáveis (*.exe;*.bat;*.lnk)", "Todos os arquivos (*.*)"))
        if not result:
            return None
        return {"path": result[0], "name": self._svc.suggest_name(result[0])}

    # ---- emuladores e ROMs ----
    def get_emulators(self):
        return self._svc.list_emulators()

    def get_emulator_form(self, emulator_id=None):
        return self._svc.get_emulator_form(emulator_id)

    def save_emulator(self, form, original_id=None):
        ok, msg, eid = self._svc.save_emulator(form, original_id)
        return {"ok": ok, "msg": msg, "id": eid}

    def delete_emulator(self, emulator_id):
        return {"ok": self._svc.delete_emulator(emulator_id)}

    def pick_roms(self, emulator_id):
        paths = self._dialog("open", self._svc.rom_file_types(emulator_id), multiple=True)
        return {"items": self._svc.rom_candidates(emulator_id, list(paths or []))}

    def scan_roms_folder(self, emulator_id):
        folder = self._dialog("folder")
        return {"items": self._svc.scan_folder(emulator_id, folder[0]) if folder else []}

    def import_roms(self, emulator_id, items):
        added, skipped = self._svc.import_roms(emulator_id, items)
        return {"ok": True, "added": added, "skipped": skipped}

    def detect_sd(self):
        cart = find_cartridge()
        if not cart:
            return {"ok": False}
        return {"ok": True, "drive": cart["drive"], "form": {
            "name": cart["name"], "description": cart["description"], "exe_path": cart["exe"],
            "cover": cart["cover"] or "", "icon": cart["icon"] or "",
            "cover_preview": load_art(cart["cover"], "cover")[0] or "",
            "icon_preview": load_art(cart["icon"], "icon")[0] or ""}}

    def get_art(self, game_id, kind):
        return self._svc.get_art(game_id, kind)

    # ---- apps do PC (aba Apps do modo console) ----
    def apps_list(self, include_missing=False):
        return self._apps.list(include_missing)

    def apps_launch(self, app_id):
        ok, msg = self._apps.launch(app_id)
        return {"ok": ok, "msg": msg}

    def apps_pick(self):
        result = self._dialog("open", ("Programas (*.exe;*.bat;*.lnk)", "Todos os arquivos (*.*)"))
        if not result:
            return {"ok": False, "msg": ""}
        ok, msg = self._apps.add(result[0])
        return {"ok": ok, "msg": "" if ok else msg}

    def apps_remove(self, app_id):
        return {"ok": self._apps.remove(app_id)}

    # ---- integração com o Windows / modo Xbox ----
    def system_info(self):
        return self._system.info()

    def system_set(self, key, value):
        ok, msg = self._system.set_option(key, value)
        return {"ok": ok, "msg": msg}

    def system_shortcut(self):
        ok, msg = self._system.create_shortcuts()
        return {"ok": ok, "msg": msg}

    # ---- RetroBat (motor de emulação) ----
    def retrobat_status(self):
        return self._rb.status()

    def retrobat_pick_root(self):
        result = self._dialog("folder")
        if not result:
            return {"ok": False, "msg": ""}
        path = result[0] if isinstance(result, (list, tuple)) else result
        ok, msg = self._rb.set_root(path)
        return {"ok": ok, "msg": msg}

    def retrobat_options(self):
        return self._rb.options() if self._rb.root() else {"global": [], "systems": []}

    def retrobat_set(self, key, value):
        return {"ok": bool(self._rb.set_option(key, value))}

    def retrobat_pick_rom(self):
        """Adicionar ROM: escolhe o arquivo e devolve os sistemas do RetroBat que aceitam essa extensão."""
        if not self._rb.root():
            return {"ok": False, "msg": "RetroBat não encontrado. Indique a pasta em Configurações → Bibliotecas."}
        result = self._dialog("open", ("Todos os arquivos (*.*)",))
        if not result:
            return None
        path = result[0]
        systems = self._rb.systems_for_file(path)
        if not systems:
            return {"ok": False, "msg": "Nenhum sistema do RetroBat aceita arquivos " + (os.path.splitext(path)[1] or "sem extensão") + "."}
        return {"ok": True, "path": path, "name": clean_rom_name(os.path.splitext(os.path.basename(path))[0]), "systems": systems}

    def retrobat_add_rom(self, path, system, name):
        s = self._rb.system(system)
        if not s or not os.path.exists(path):
            return {"ok": False, "msg": "Sistema ou arquivo inválido."}
        item = {"store": "retrobat", "store_id": f"{system}:{os.path.normcase(path)}", "name": (name or "").strip(),
                "install_path": "", "launch_type": "retrobat", "launch_uri": "", "exe_path": "", "rom_path": path,
                "rb_system": system, "rb_system_name": s["fullname"], "platform": s["fullname"], "size": "",
                "cover_src": "", "hero_src": "", "rating": ""}
        before = set(self._svc.catalog)
        added, _ = self._libs.import_items([item])
        new = [k for k in self._svc.catalog if k not in before]
        return {"ok": bool(added), "msg": "" if added else "Não consegui adicionar a ROM.", "id": new[0] if new else ""}

    def retrobat_game_emus(self, game_id):
        g = self._svc.catalog.get(game_id) or {}
        cur = g.get("rb_emulator", "")
        cur = f"{cur}/{g.get('rb_core')}" if cur and g.get("rb_core") else cur
        return {"choices": self._rb.game_emulators(g.get("rb_system", "")), "value": cur}

    def retrobat_game_set(self, game_id, value):
        emu, _, core = (value or "").partition("/")
        if emu:
            self._svc.update_game(game_id, {"rb_emulator": emu, "rb_core": core})
        else:
            self._svc.update_game(game_id, {}, remove=("rb_emulator", "rb_core"))
        return {"ok": True}

    def retrobat_emulators(self):
        return self._rb.emulators(thumbs=self._rb_thumb)

    def _rb_thumb(self, path):
        """Logo do emulador reduzido (256 px) e servido pelo servidor local de imagens."""
        try:
            return self._bd.thumb(path, 256)
        except Exception:
            return ""

    def retrobat_open_emulator(self, emu_id):
        ok, msg = self._rb.open_emulator(emu_id)
        return {"ok": ok, "msg": msg}

    def retrobat_test(self):
        """Diagnóstico: controles vistos pelo SDL do RetroBat e o comando que seria usado."""
        pads = self._rb.controllers()
        return {"pads": pads, "args": " ".join(self._rb.controller_args(pads))}

    # ---- conquistas (Steam) ----
    def _steam_appid(self, game_id):
        g = self._svc.catalog.get(game_id) or {}
        sid = str(g.get("store_id") or "")
        return sid if g.get("store") == "steam" and sid.isdigit() else None

    def _retro(self, game_id):
        """Jogo do RetroBat num sistema que existe no RetroAchievements (senão None)."""
        g = self._svc.catalog.get(game_id) or {}
        return g if g.get("launch_type") == "retrobat" and g.get("rb_system") in RA_CONSOLES else None

    def achievements_summary(self, game_id):
        g = self._retro(game_id)
        if g:
            return {**self._ra.summary(g), "supported": True}
        appid = self._steam_appid(game_id)
        if not appid:
            return {"ok": False, "supported": False}
        return {**self._ach.summary(appid), "supported": True, "source": "steam"}

    def achievements(self, game_id):
        g = self._retro(game_id)
        if g:
            return {**self._ra.game(g, fresh=True), "supported": True, "source": "ra"}
        appid = self._steam_appid(game_id)
        if not appid:
            return {"ok": False, "supported": False, "reason": "Por enquanto, só jogos da Steam e do RetroBat têm conquistas no NeoSD."}
        return {**self._ach.game(appid), "supported": True, "source": "steam"}

    # ---- RetroAchievements (conta) ----
    def ra_status(self):
        return self._ra.status()

    def ra_configure(self, data):
        data = data or {}
        st = self._ra.configure(user=data.get("user"), password=data.get("password") or None, key=data.get("key") or None,
                                hardcore=data.get("hardcore"), enabled=data.get("enabled"))
        test = self._ra.test(password=data.get("password") or None) if self._ra.configured() else {"ok": False, "msg": ""}
        return {**st, "test": test}

    def ra_test(self):
        return self._ra.test()

    # ---- sons do modo console ----
    def sfx_play(self, key):
        return self._sfx.play(str(key))

    def sfx_preload(self):
        self._sfx.preload()
        return True

    def sfx_status(self):
        return self._sfx.status()

    def sfx_set(self, key, value):
        if key == "on":
            self._app.set("sfx_on", bool(value))
        elif key == "vol":
            self._app.set("sfx_vol", max(0, min(100, int(value))))
        elif key == "intro":
            self._app.set("console_intro", bool(value))
        return self._sfx.status()

    def sfx_open_folder(self):
        st = self._sfx.status()
        try:
            Path(st["folder"]).mkdir(parents=True, exist_ok=True)
            os.startfile(st["folder"])  # type: ignore[attr-defined]
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def ach_config(self):
        return {"overlay": bool(self._app.get("ach_overlay", True)), "sound": bool(self._app.get("ach_sound", True))}

    def ach_set(self, key, value):
        if key in ("overlay", "sound"):
            self._app.set("ach_" + key, bool(value))
        return self.ach_config()

    def ach_debug(self, game_id=None, delay=0):
        """Debug: simula o desbloqueio de uma conquista REAL de um jogo da Steam (nome e ícone de verdade), passando
        pelo mesmo caminho de uma conquista nova. Nada é alterado na Steam. delay dá tempo de abrir o jogo."""
        import random
        appid = self._steam_appid(game_id) if game_id else None
        apps = [appid] if appid else [str(g.get("store_id")) for g in list(self._svc.catalog.values())
                                       if g.get("store") == "steam" and str(g.get("store_id") or "").isdigit()]
        random.shuffle(apps)
        pick = None
        for a in apps:
            sc = self._ach.schema(a)
            if sc and sc["items"]:
                u = self._ach.user(a)
                locked = [i for i in sc["items"] if not u or i["key"] not in u[0]]
                pick = (a, random.choice(locked or sc["items"]))
                break
        if not pick:  # nenhum jogo da Steam com conquistas: usa uma de exemplo
            pick = ("0", {"name": "Conquista de teste", "desc": "Simulação do NeoSD", "icon": "", "icon_gray": ""})
        delay = max(0, min(60, int(delay or 0)))

        def go():
            time.sleep(delay)
            self._announce([(pick[0], pick[1], int(time.time()))], debug=True)

        threading.Thread(target=go, daemon=True).start()
        g = next((x for x in self._svc.catalog.values() if str(x.get("store_id")) == pick[0] and x.get("store") == "steam"), {})
        return {"ok": True, "name": pick[1]["name"], "game": g.get("name", ""), "delay": delay}

    def ach_test(self):
        ok = self._overlay.show("Conquista de teste", "NeoSD · assim o aviso aparece por cima do jogo", "", "")
        return {"ok": ok, "msg": "" if ok else "O aviso por cima do jogo está desligado."}

    # ---- baixar e instalar o RetroBat oficial ----
    def retrobat_setup_status(self):
        return self._rbs.status()

    def retrobat_setup_start(self):
        ok, msg = self._rbs.start()
        return {"ok": ok, "msg": msg}

    def retrobat_setup_cancel(self):
        return {"ok": self._rbs.cancel()}

    def open_page(self, which):
        """Abre no navegador só páginas conhecidas (nunca um endereço vindo da interface)."""
        import webbrowser
        url = {"retrobat": "https://www.retrobat.org/", "retrobat_releases": self._rbs.st["page"],
               "ra_settings": "https://retroachievements.org/settings", "ra_site": "https://retroachievements.org/"}.get(which)
        if which == "update":  # página do Release (só endereços do GitHub)
            page = ((self._upd.st.get("latest") or {}).get("page") or "")
            url = page if page.startswith("https://github.com/") else None
        if url:
            webbrowser.open(url)
        return {"ok": bool(url)}

    # ---- rede: compartilhar as ROMs ----
    def net_status(self):
        return self._net.status()

    def net_enable(self, make_private=False):
        ok, msg = self._net.enable(bool(make_private))
        return {"ok": ok, "msg": msg}

    def net_disable(self):
        ok, msg = self._net.disable()
        return {"ok": ok, "msg": msg}

    def net_open(self):
        ok, msg = self._net.open_folder()
        return {"ok": ok, "msg": msg}

    def net_set_auto(self, value):
        self._app.set("net_auto_import", bool(value))
        return {"ok": True}

    # ---- AnyFSE (modo Xbox) ----
    def anyfse_status(self):
        return self._anyfse.status()

    def anyfse_install(self):
        ok, msg = self._anyfse.install()
        return {"ok": ok, "msg": msg}

    def anyfse_open(self):
        ok, msg = self._anyfse.open_settings()
        return {"ok": ok, "msg": msg}

    # ---- fundo do modo console / papel de parede ----
    def console_bg(self, game_id, w, h):
        return self._bd.game(game_id, w, h)

    def bg_config(self):
        return self._bd.config()

    def bg_set(self, key, value):
        return {"ok": self._bd.set(key, value)}

    def wallpaper(self, wall_id, w, h):
        return self._bd.wallpaper(wall_id, w, h)

    def wallpaper_add(self):
        result = self._dialog("open", ("Imagens (*.jpg;*.jpeg;*.png;*.webp;*.bmp)",), multiple=True)
        if not result:
            return {"ok": False, "msg": ""}
        added, last_err = [], ""
        for path in result:
            ok, val = self._bd.add(path)
            if ok:
                added.append(val)
            else:
                last_err = val
        if added:
            self._bd.set("wall", added[-1])
            self._bd.set("mode", "wallpaper")
        return {"ok": bool(added), "msg": last_err, "id": added[-1] if added else ""}

    def wallpaper_remove(self, wall_id):
        return {"ok": self._bd.remove(wall_id)}

    # ---- atualizações (GitHub Releases) ----
    def update_status(self):
        return self._upd.status()

    def update_check(self):
        threading.Thread(target=self._upd.check, daemon=True).start()
        return {"ok": True}

    def update_download(self):
        return {"ok": self._upd.download()}

    def update_install(self):
        ok, msg = self._upd.install("--console" if self._fullscreen else "")
        return {"ok": ok, "msg": msg}

    def update_set(self, key, value):
        if key in ("auto", "beta"):
            self._app.set("update_" + key, bool(value))
            if key == "beta":
                threading.Thread(target=self._upd.check, daemon=True).start()
        return self._upd.status()

    def app_exit(self):
        if self._window:
            self._window.destroy()

    # ---- bibliotecas do PC + primeiros passos ----
    def libraries_status(self):
        return self._libs.status()

    def libraries_scan(self, store):
        try:
            return {"items": self._libs.scan(store)}
        except Exception as e:
            return {"ok": False, "msg": f"Não consegui escanear: {e}", "items": []}

    def libraries_scan_folder(self):
        folder = self._dialog("folder")
        if not folder:
            return {"items": [], "cancelled": True}
        return {"items": self._libs.scan(folder=folder[0], store="folder")}

    def libraries_import(self, items):
        added, skipped = self._libs.import_items(items)
        return {"ok": True, "added": added, "skipped": skipped}

    def get_app_state(self):
        sc = self._scraper
        rb = self._rb.root()
        return {"first_run": self._app.first_run, "games": len(self._svc.catalog), "emulators": len(self._svc.emulators),
                "scraper_ready": any(p.configured() for p in (sc.igdb, sc.ss, sc.sgdb)),
                "console": bool(self._fullscreen), "retrobat": bool(rb), "rb_games": sum(1 for g in self._svc.catalog.values() if g.get("launch_type") == "retrobat")}

    def about(self):
        """Versão do NeoSD e do RetroBat (seção Sobre / créditos)."""
        try:
            ver = (Path(getattr(sys, "_MEIPASS", HERE)) / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            ver = ""
        rb = self._rb.root()
        return {"version": ver, "retrobat": self._rb.version() if rb else "", "retrobat_found": bool(rb)}

    def finish_onboarding(self):
        self._app.finish_onboarding()
        return {"ok": True}

    # ---- scraper ----
    def _safe(self, fn, *a):
        try:
            return fn(*a)
        except ScraperError as e:
            return {"ok": False, "msg": str(e)}

    def scraper_get_config(self):
        return {**self._scraper.get_config(), "pending": self._scraper.pending(),
                "running": self._scraper.batch_running()}

    def scraper_save_config(self, data):
        self._scraper.save_config(data)
        return {"ok": True}

    def scraper_test(self, provider_id):
        ok, msg = self._scraper.test(provider_id)
        return {"ok": ok, "msg": msg}

    def scraper_search(self, game_id, query=None):
        return self._safe(self._scraper.search, game_id, query)

    def scraper_preview(self, game_id, candidate):
        return self._safe(self._scraper.preview, game_id, candidate)

    def scraper_apply(self, game_id, selection):
        return self._safe(self._scraper.apply, game_id, selection)

    def scraper_batch_start(self):
        ok, msg, total = self._scraper.batch_start(lambda p: self._emit("scraperProgress", p))
        return {"ok": ok, "msg": msg, "total": total}

    def scraper_batch_cancel(self):
        self._scraper.batch_cancel()
        return {"ok": True}

    def set_fullscreen(self, value):
        if self._window and bool(value) != self._fullscreen:
            self._window.toggle_fullscreen()
            self._fullscreen = bool(value)


def take_install_flag():
    """O instalador do NeoSD grava HKCU\\Software\\NeoSD\\InstallRetroBat=1 quando a opção do RetroBat é marcada."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\NeoSD", 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            val = winreg.QueryValueEx(k, "InstallRetroBat")[0]
            winreg.DeleteValue(k, "InstallRetroBat")
            return bool(val)
    except (ImportError, OSError):
        return False


def neosd_in_front():
    """A janela em primeiro plano é do NeoSD? (se não for, provavelmente é o jogo)"""
    if os.name != "nt":
        return True
    try:
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        pid = ctypes.c_ulong()
        u.GetWindowThreadProcessId(ctypes.c_void_p(u.GetForegroundWindow()), ctypes.byref(pid))
        return pid.value == os.getpid()
    except Exception:
        return True


def js(window, code):
    try:
        window.evaluate_js(code)
    except Exception:
        pass  # janela fechando / página ainda carregando


def dark_titlebar(window):
    try:
        hwnd = window.native.Handle.ToInt32()
        value = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (build nova / antiga)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr, ctypes.byref(value), 4) == 0:
                break
    except Exception:
        pass


def setup_log():
    """Erros vão para library/neosd.log (até ~1 MB, com 1 backup) para dar pra investigar problemas."""
    import logging
    from logging.handlers import RotatingFileHandler
    if logging.getLogger("neosd").handlers:
        return
    try:
        LIBRARY.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(LIBRARY / "neosd.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger("neosd").addHandler(h)
        logging.getLogger("neosd").setLevel(logging.INFO)
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--console", action="store_true", help="abre direto no modo console (tela cheia)")
    parser.add_argument("--install-retrobat", action="store_true", help="baixa e instala o RetroBat oficial ao abrir")
    args, _ = parser.parse_known_args()
    setup_log()
    app = AppSettings()
    console_start = args.console or bool(app.get("start_console"))
    if not instance.acquire():  # já está aberto: só traz a janela pra frente (e pro modo console, se pedido)
        instance.signal_existing(console_start)
        return
    updater = Updater(app, DATA / "updates")
    if updater.auto() and updater.can_install() and updater.ready_file():
        # atualização já baixada: instala agora, antes de abrir (o instalador reabre o NeoSD no fim)
        updater.quit = lambda: None
        ok, msg = updater.install("--console" if console_start else "")
        if ok:
            logging.getLogger("neosd").info("abrindo o instalador da atualização e saindo")
            return
        logging.getLogger("neosd").warning("atualização ao abrir: %s", msg)
    service = GameService()
    scraper = ScraperService(service)
    libs = LibraryService(service)
    retrobat = RetroBatService(app)
    service.retrobat = libs.retrobat = retrobat
    from core.retrobat import NOT_GAME_SYSTEMS
    purged = service.purge_systems(NOT_GAME_SYSTEMS) + service.purge_duplicates()
    if purged:
        logging.getLogger("neosd").info("removidos %d itens do RetroBat que não são jogos (menu de emuladores)", purged)
    backdrop = BackdropService(app, DATA, service.art_path, MediaServer().start())
    system = SystemService(app, HERE)
    api = Api(service, scraper, libs, app, AppsService(libs), system, backdrop)
    api._anyfse = AnyFSEService(system)
    api._rb = retrobat
    api._net = NetworkService(app, retrobat)
    api._upd = updater
    updater.notify = lambda st: api._emit("updateStatus", st)
    updater.quit = api.app_exit
    ach = api._ach = SteamAchievements(libs.steam_root, LIBRARY)
    overlay = api._overlay = OverlayService(LIBRARY / "achicons", app)
    ra = api._ra = RetroAchievementsService(app, retrobat, LIBRARY)
    retrobat.on_launch = ra.on_launch
    discs = service.discs = api._discs = DiscService(retrobat, app)
    discs.on_launch = ra.on_launch  # conquistas por cima do jogo também nos discos
    app_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else HERE
    overlay.sfx = api._sfx = SfxService([DATA / "SFX", app_dir / "SFX"], app)
    rbsetup = api._rbs = RetroBatSetup(retrobat, DATA / "downloads", notify=lambda st: api._emit("rbSetup", st))
    window = webview.create_window("NEO SD", str(UI_FILE), js_api=api, width=1360, height=780,
                                   min_size=(1000, 640), background_color="#07070d", fullscreen=console_start)
    api._window = window
    api._fullscreen = console_start
    api._emit = lambda fn, payload: js(window, f"{fn}({json.dumps(payload)})")

    def games_changed(msg):
        js(window, f"gamesChanged({json.dumps(msg)})")

    def on_insert(drive):
        cart = read_cartridge(drive)
        if not cart:
            js(window, 'toast("● game.exe/game.bat não encontrado no cartão",1)')
            return
        name, is_new = service.import_cartridge(cart)
        games_changed(f"● Jogo adicionado: {name}" if is_new else f"● SD detectado: {drive.rstrip(chr(92) + '/')}")
        if is_new and scraper.cfg.get("auto_on_insert") and scraper.available():
            threading.Thread(target=auto_scrape, args=(name,), daemon=True).start()

    def auto_scrape(name):  # cartão novo: busca capa/sinopse/ficha sozinho (só aplica se o acerto for inequívoco)
        try:
            r = scraper.auto(name)
        except ScraperError as e:
            return js(window, f"toast({json.dumps('Scraper: ' + str(e))},1)")
        msg = {"applied": f"● Dados de {name} completados", "review": f"● {name}: abra Buscar dados para escolher o jogo certo",
               "notfound": f"● {name}: nada encontrado online"}.get(r["status"], "")
        games_changed(msg)

    def rb_installed(root):  # RetroBat recém-instalado: já traz os jogos (o RetroBat vem com alguns gratuitos)
        try:
            new = [i for i in libs.scan("retrobat") if not i["already"]]
            added = libs.import_items(new)[0] if new else 0
        except Exception:
            logging.getLogger("neosd").exception("importar jogos do RetroBat recém-instalado")
            added = 0
        games_changed(f"● RetroBat pronto{f': {added} jogo' + ('s' if added != 1 else '') + ' adicionado' + ('s' if added != 1 else '') if added else ''}")

    rbsetup.on_installed = rb_installed

    def install_start():  # em tela cheia o instalador do RetroBat ficaria escondido atrás do NeoSD
        if api._fullscreen:
            rbsetup._was_full = True
            window.minimize()

    def install_end():
        if getattr(rbsetup, "_was_full", False):
            rbsetup._was_full = False
            on_combo()

    rbsetup.on_install_start, rbsetup.on_install_end = install_start, install_end

    def on_disc_insert(drive):  # disco no leitor: reconhece, coloca na biblioteca e destaca (como num console)
        info = discs.insert(drive)
        letter = drive.rstrip("\\")
        if not info:
            return
        if info["system"] not in PLAYABLE:
            js(window, f"toast({json.dumps('💿 ' + (info['why'] or 'Disco não reconhecido.'))},1)")
            return
        gid, name, is_new = service.import_disc(info)
        games_changed(f"💿 {name} no leitor {letter}" + ("" if info["playable"] else f" · {info['why']}"))
        js(window, f"discIn({json.dumps({'id': gid, 'name': name, 'playable': info['playable'], 'why': info['why'], 'need': info.get('need', '')})})")
        if is_new and scraper.cfg.get("auto_on_insert") and scraper.available():
            threading.Thread(target=auto_scrape, args=(gid,), daemon=True).start()

    def on_disc_remove(drive):
        discs.remove(drive)
        games_changed("💿 Disco retirado do leitor")

    def on_remove(_drives):
        games_changed("● SD removido")

    def on_tick(drive):
        js(window, f"sdTick({json.dumps(disk_info(drive) if drive else None)})")

    def on_combo():  # Start + Back
        try:
            window.restore()
            window.on_top = True
            window.on_top = False
        except Exception:
            pass
        js(window, "consoleWake()")

    def rom_watch():
        """ROMs novas nas pastas do RetroBat (copiadas pela rede, por exemplo) entram sozinhas na biblioteca.
        Arquivos alterados há menos de 30 s são ignorados até a cópia terminar."""
        window.events.loaded.wait(30)
        while True:
            try:
                if app.get("net_auto_import", True) and retrobat.root():
                    now = time.time()
                    new = [i for i in libs.scan("retrobat") if not i["already"]
                           and now - os.path.getmtime(i["rom_path"]) > 30]
                    if new:
                        added, _ = libs.import_items(new)
                        if added:
                            games_changed(f"● {added} jogo{'s' if added > 1 else ''} novo{'s' if added > 1 else ''} do RetroBat adicionado{'s' if added > 1 else ''}")
                            if scraper.cfg.get("auto_on_insert") and scraper.available():
                                for it in new[:10]:
                                    threading.Thread(target=auto_scrape, args=(it["name"],), daemon=True).start()
            except Exception:
                logging.getLogger("neosd").exception("verificação de ROMs novas")
            time.sleep(45)

    def announce(new, debug=False):
        """Mostra conquistas novas: jogo na frente -> janela própria por cima dele; NeoSD na frente -> dentro do NeoSD.
        new = [(appid, item do esquema, data)]. debug=True é a simulação de Configurações (nada muda na Steam)."""
        by_app = {str(g.get("store_id")): (gid, g) for gid, g in list(service.catalog.items()) if g.get("store") == "steam"}
        out, front = [], neosd_in_front()
        for appid, it, when in new:
            gid, g = by_app.get(appid, (None, {}))
            icon = it.get("icon") or it.get("icon_gray") or ""
            out.append({"game_id": gid, "game": g.get("name") or (ach.schema(appid) or {}).get("name", "") or "NeoSD",
                        "name": it["name"], "desc": it.get("desc", ""), "time": when, "ikey": f"{appid}_{icon}",
                        "icon": f"https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon}" if icon else ""})
        show_unlocks(out, debug, front)

    def show_unlocks(out, debug=False, front=None):
        front = neosd_in_front() if front is None else front
        label = "CONQUISTA DESBLOQUEADA · TESTE" if debug else "CONQUISTA DESBLOQUEADA"
        shown = not front and all([overlay.show(o["name"], o["game"], o["icon"], o["ikey"], label) for o in out])
        logging.getLogger("neosd").info("conquistas %s(%s): %s", "de teste " if debug else "novas ",
                                        "por cima do jogo" if shown else "no NeoSD", [(o["game"], o["name"]) for o in out])
        api._emit("achUnlocked", {"items": out, "popup": not shown, "debug": debug})

    api._announce = announce
    ra.announce = lambda items: show_unlocks([{"game_id": None, **i} for i in items])  # RetroAchievements

    def ach_watch():
        """Conquistas novas da Steam: a Steam regrava appcache/stats no instante do desbloqueio."""
        window.events.loaded.wait(30)
        while True:
            try:
                new = ach.poll()
                if new:
                    announce(new)
            except Exception:
                logging.getLogger("neosd").exception("verificação de conquistas")
            time.sleep(2)

    def wake_loop():  # 2ª execução pediu atenção: traz a janela e, se for o caso, entra no modo console
        while True:
            time.sleep(0.7)
            w = instance.poll_wake()
            if w:
                on_combo()
                if w.get("console"):
                    js(window, "consoleBoot()")

    def boot():
        window.events.loaded.wait(15)
        if console_start:
            js(window, "consoleBoot()")  # kiosk: sem 'sair' acidental pelo B do controle
        threading.Thread(target=wake_loop, daemon=True).start()
        if args.install_retrobat or take_install_flag():  # pedido pelo instalador do NeoSD (ou pela linha de comando)
            if not retrobat.root():
                rbsetup.start()
        threading.Thread(target=rom_watch, daemon=True).start()
        threading.Thread(target=ach_watch, daemon=True).start()
        updater.start()
        dark_titlebar(window)
        SDMonitor(on_insert, on_remove, on_tick).start()
        DiscMonitor(on_disc_insert, on_disc_remove).start()
        watch_combo(on_combo)

    webview.start(boot)


def fatal(exc):
    """Erro na abertura: registra no log e mostra uma janela (no .exe não há terminal para ver o erro)."""
    import logging
    import traceback
    logging.getLogger("neosd").error("falha ao iniciar\n%s", "".join(traceback.format_exception(exc)))
    msg = (f"O NeoSD não conseguiu abrir.\n\n{type(exc).__name__}: {exc}\n\n"
           "Se o erro falar de WebView2, instale o Microsoft Edge WebView2 Runtime "
           "(go.microsoft.com/fwlink/p/?LinkId=2124703).\n\n"
           f"Detalhes em: {LIBRARY / 'neosd.log'}")
    try:
        ctypes.windll.user32.MessageBoxW(None, msg, "NEO SD", 0x10)
    except Exception:
        print(msg, file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        setup_log()
        fatal(e)
        sys.exit(1)
