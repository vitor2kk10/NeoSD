"""Aba 'Apps' do modo console: lojas/launchers instalados no PC + apps personalizados.
No modo Xbox (tela cheia) é difícil abrir Steam, Epic etc. sem voltar ao desktop; aqui eles ficam a um botão."""
import os
import shlex
import subprocess
import sys
import threading
import uuid

from .icons import icon_for
from .media import hash_hue
from .store import load_json, save_json

FILE = "library/apps.json"
STORES = (("steam", "Steam", 205), ("steam_bp", "Steam Big Picture", 205), ("epic", "Epic Games", 230),
          ("gog", "GOG Galaxy", 285), ("ea", "EA app", 8), ("ubisoft", "Ubisoft Connect", 212),
          ("battlenet", "Battle.net", 200), ("xbox", "Xbox", 125), ("msstore", "Microsoft Store", 195))
XBOX_APP = r"shell:AppsFolder\Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App"


class AppsService:
    def __init__(self, libs, env=None, win=None, icons=None):
        self.libs = libs
        self.icons = icons or icon_for
        self.env = os.environ if env is None else env
        self.win = (sys.platform == "win32") if win is None else win
        self.lock = threading.RLock()
        self.custom = list(load_json(FILE).get("apps", []))

    # ---------- detecção ----------
    def _first_exe(self, *rels):
        for base in (self.env.get("ProgramFiles", r"C:\Program Files"), self.env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            for rel in rels:
                p = os.path.join(base, *rel.split("/"))
                if os.path.isfile(p):
                    return p
        return ""

    def _reg_exe(self, key, value, exe):
        d = (self.libs.reg.values("HKLM", key) or {}).get(value)
        p = os.path.join(d, exe) if d else ""
        return p if p and os.path.isfile(p) else ""

    def detect(self):
        """-> {id: (tipo, destino, exe_do_ícone)} só do que está instalado. tipo: exe | uri | shell."""
        found = {}
        root = self.libs.steam_root()
        if root and os.path.isfile(os.path.join(root, "steam.exe")):
            steam = os.path.join(root, "steam.exe")
            found["steam"] = ("exe", steam, steam)
            found["steam_bp"] = ("uri", "steam://open/bigpicture", steam)
        epic = self._first_exe("Epic Games/Launcher/Portal/Binaries/Win32/EpicGamesLauncher.exe",
                               "Epic Games/Launcher/Portal/Binaries/Win64/EpicGamesLauncher.exe")
        if epic:
            found["epic"] = ("exe", epic, epic)
        elif self.libs.epic_dir():
            found["epic"] = ("uri", "com.epicgames.launcher://", "")
        gog = self._reg_exe(r"SOFTWARE\WOW6432Node\GOG.com\GalaxyClient\paths", "client", "GalaxyClient.exe") \
            or self._first_exe("GOG Galaxy/GalaxyClient.exe")
        if gog:
            found["gog"] = ("exe", gog, gog)
        ea = self._first_exe("Electronic Arts/EA Desktop/EA Desktop/EADesktop.exe")
        if ea:
            found["ea"] = ("exe", ea, ea)
        ubi = self._reg_exe(r"SOFTWARE\WOW6432Node\Ubisoft\Launcher", "InstallDir", "UbisoftConnect.exe") \
            or self._first_exe("Ubisoft/Ubisoft Game Launcher/UbisoftConnect.exe")
        if ubi:
            found["ubisoft"] = ("exe", ubi, ubi)
        bnet = self._first_exe("Battle.net/Battle.net Launcher.exe")
        if bnet:
            found["battlenet"] = ("exe", bnet, bnet)
        if self.win:  # apps da Microsoft: sempre presentes no Windows
            found["xbox"] = ("shell", XBOX_APP, "")
            found["msstore"] = ("uri", "ms-windows-store:", "")
        return found

    def list(self, include_missing=False):
        found = self.detect()
        out = [{"id": i, "name": n, "kind": "store", "found": i in found, "hue": h,
                "icon": self.icons(found[i][2]) if i in found else None}
               for i, n, h in STORES if include_missing or i in found]
        with self.lock:
            for c in self.custom:
                out.append({"id": c["id"], "name": c["name"], "kind": "custom", "found": os.path.exists(c["path"]),
                            "hue": hash_hue(c["name"]),
                            "icon": self.icons(c["path"]) if c["path"].lower().endswith(".exe") else None})
        return [a for a in out if include_missing or a["found"]]

    # ---------- apps personalizados ----------
    def add(self, path, name=None):
        path = (path or "").strip()
        if not os.path.exists(path):
            return False, "Arquivo não encontrado."
        label = (name or "").strip() or os.path.splitext(os.path.basename(path))[0]
        with self.lock:
            self.custom.append({"id": "c_" + uuid.uuid4().hex[:8], "name": label, "path": path})
            save_json(FILE, {"apps": self.custom})
        return True, label

    def remove(self, app_id):
        with self.lock:
            n = len(self.custom)
            self.custom = [c for c in self.custom if c["id"] != app_id]
            if len(self.custom) != n:
                save_json(FILE, {"apps": self.custom})
        return len(self.custom) != n

    # ---------- abrir ----------
    def launch(self, app_id):
        with self.lock:
            custom = next((c for c in self.custom if c["id"] == app_id), None)
        try:
            if custom:
                kind, target = ("exe", custom["path"]) if custom["path"].lower().endswith((".exe", ".bat", ".cmd")) else ("uri", custom["path"])
                if not os.path.exists(custom["path"]):
                    return False, "O arquivo do app não existe mais."
            else:
                kind, target = (self.detect().get(app_id) or (None, None))[:2]
                if not kind:
                    return False, "App não encontrado neste PC."
            if kind == "exe":
                subprocess.Popen([target], cwd=os.path.dirname(target) or None)
            elif kind == "shell":
                subprocess.Popen(["explorer.exe", target])
            else:
                start = getattr(os, "startfile", None)
                if start is None:
                    return False, "Abrir apps só funciona no Windows."
                start(target)
            return True, ""
        except Exception as e:
            return False, f"Não consegui abrir: {e}"
