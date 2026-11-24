"""Scanners das bibliotecas de jogos instaladas no PC: Steam, GOG, Epic Games e pasta personalizada.
Nada é copiado: o launcher só guarda ONDE cada jogo está (e a capa local da Steam, se existir)."""
import difflib
import glob
import json
import ntpath
import os
import re
import threading

try:
    import winreg
except ImportError:  # fora do Windows (testes): sem registro
    winreg = None

from .games import METADATA_KEYS, clean_rom_name, safe_name

STORE_NAMES = {"steam": "Steam", "gog": "GOG", "epic": "Epic Games", "folder": "Pasta", "retrobat": "RetroBat"}
ALLOWED_URI = ("steam://", "com.epicgames.launcher://")
STEAM_SKIP_IDS = {"228980"}  # Steamworks Common Redistributables
STEAM_SKIP_NAMES = re.compile(r"^(proton\b|steam linux runtime|steamworks)", re.I)
EXE_SKIP = re.compile(r"(unins|uninst|setup|install|redist|vcredist|vc_redist|dxsetup|dotnet|crashhandler|crashreport|"
                      r"unitycrash|prereq|cleanup|updater|7z|notification|helper|ue4prereq)", re.I)


# ---------- registro do Windows (injetável) ----------
class WinReg:
    HIVES = {"HKLM": "HKEY_LOCAL_MACHINE", "HKCU": "HKEY_CURRENT_USER"}

    def _open(self, hive, path):
        return winreg.OpenKey(getattr(winreg, self.HIVES[hive]), path)

    def values(self, hive, path):
        if winreg is None:
            return None
        try:
            with self._open(hive, path) as k:
                out, i = {}, 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(k, i)
                    except OSError:
                        return out
                    out[name] = val
                    i += 1
        except OSError:
            return None

    def subkeys(self, hive, path):
        if winreg is None:
            return []
        try:
            with self._open(hive, path) as k:
                out, i = [], 0
                while True:
                    try:
                        out.append(winreg.EnumKey(k, i))
                    except OSError:
                        return out
                    i += 1
        except OSError:
            return []


# ---------- formatos ----------
def parse_vdf(text):
    """Parser do formato KeyValues da Valve (libraryfolders.vdf, appmanifest_*.acf)."""
    toks = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
    pos = 0

    def obj():
        nonlocal pos
        d = {}
        while pos < len(toks):
            s, brace = toks[pos]
            if brace == "}":
                pos += 1
                return d
            key, pos = s, pos + 1
            if pos >= len(toks):
                break
            s2, b2 = toks[pos]
            if b2 == "{":
                pos += 1
                d[key] = obj()
            else:
                d[key] = re.sub(r"\\(.)", r"\1", s2)
                pos += 1
        return d
    return obj()


def fmt_size(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB" if n else ""


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _first(patterns):
    for p in patterns:
        m = sorted(glob.glob(p))
        if m:
            return m[0]
    return ""


def _norm(p):
    return os.path.normcase(os.path.normpath(p)) if p else ""


def pick_exe(folder, max_depth=2):
    """Executável principal de uma pasta de jogo: ignora instaladores/crash handlers e prefere
    nome parecido com o da pasta, arquivo grande e pouca profundidade."""
    name = os.path.basename(folder.rstrip("\\/")).lower()
    best, best_score = "", float("-inf")
    base = folder.rstrip("\\/").count(os.sep)
    for root, dirs, files in os.walk(folder):
        depth = root.count(os.sep) - base
        if depth >= max_depth:
            dirs[:] = []
        for f in files:
            stem, ext = os.path.splitext(f)
            if ext.lower() not in (".exe", ".bat") or EXE_SKIP.search(f):
                continue
            try:
                size = os.path.getsize(os.path.join(root, f))
            except OSError:
                continue
            score = (difflib.SequenceMatcher(None, re.sub(r"\W", "", stem.lower()), re.sub(r"\W", "", name)).ratio() * 3
                     + min(size / 50e6, 2.0) - depth * 0.7 - (0.8 if ext.lower() == ".bat" else 0))
            if score > best_score:
                best, best_score = os.path.join(root, f), score
    return best


def clean_folder_name(n):
    n = clean_rom_name(n.replace("_", " "))
    if " " not in n and n.count(".") >= 2:  # The.Witcher.3 -> The Witcher 3
        n = n.replace(".", " ")
    return re.sub(r"\s*-\s*(gog|goG|repack|setup)$", "", n, flags=re.I).strip() or n


# ---------- serviço ----------
class LibraryService:
    def __init__(self, games, reg=None, steam_root=None, programdata=None):
        self.games, self.reg = games, reg or WinReg()
        self._steam_root, self._programdata = steam_root, programdata
        self.retrobat = None  # RetroBatService (ligado no main)
        self._import_lock = threading.Lock()  # escaneamento manual e importação automática não rodam juntos

    # -- localizar lojas
    def steam_root(self):
        if self._steam_root is not None:
            return self._steam_root if os.path.isdir(self._steam_root or "") else ""
        for hive, path, key in (("HKCU", r"Software\Valve\Steam", "SteamPath"),
                                ("HKLM", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
            v = (self.reg.values(hive, path) or {}).get(key)
            if v and os.path.isdir(v):
                return os.path.normpath(v)
        guess = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam")
        return guess if os.path.isdir(guess) else ""

    def steam_libraries(self, root):
        libs = [root]
        vdf = parse_vdf(_read(os.path.join(root, "steamapps", "libraryfolders.vdf")))
        for v in (vdf.get("libraryfolders") or vdf.get("LibraryFolders") or {}).values():
            p = v.get("path") if isinstance(v, dict) else v if isinstance(v, str) else None
            if p and os.path.isdir(p) and _norm(p) not in [_norm(x) for x in libs]:
                libs.append(os.path.normpath(p))
        return libs

    def epic_dir(self):
        pd = self._programdata if self._programdata is not None else os.environ.get("ProgramData", r"C:\ProgramData")
        d = os.path.join(pd, "Epic", "EpicGamesLauncher", "Data", "Manifests")
        return d if os.path.isdir(d) else ""

    GOG_KEY = r"SOFTWARE\WOW6432Node\GOG.com\Games"

    def status(self):
        root = self.steam_root()
        gog = self.reg.subkeys("HKLM", self.GOG_KEY)
        epic = self.epic_dir()
        return [
            {"id": "steam", "name": "Steam", "found": bool(root), "detail": root,
             "hint": f"{len(self.steam_libraries(root))} biblioteca(s)" if root else ""},
            {"id": "gog", "name": "GOG", "found": bool(gog), "detail": "Registro do Windows" if gog else "",
             "hint": f"{len(gog)} item(ns) registrado(s)" if gog else ""},
            {"id": "epic", "name": "Epic Games", "found": bool(epic), "detail": epic, "hint": ""},
        ] + self._retrobat_status()

    def _retrobat_status(self):
        rb = self.retrobat
        root = rb.root() if rb else None
        if not root:
            return [{"id": "retrobat", "name": "RetroBat", "found": False, "detail": "", "hint": ""}]
        systems = rb.summary()
        n = sum(x["count"] for x in systems)
        return [{"id": "retrobat", "name": "RetroBat", "found": True, "detail": str(root),
                 "hint": f"{n} jogo(s) em {len(systems)} sistema(s)"}]

    # -- scanners
    def _steam_art(self, root, appid):
        cache = glob.escape(os.path.join(root, "appcache", "librarycache"))
        cover = _first([f"{cache}/{appid}_library_600x900.jpg", f"{cache}/{appid}/library_600x900.jpg",
                        f"{cache}/{appid}/*/library_600x900.jpg"])
        hero = _first([f"{cache}/{appid}_library_hero.jpg", f"{cache}/{appid}/library_hero.jpg",
                       f"{cache}/{appid}/*/library_hero.jpg"])
        return cover, hero

    def scan_steam(self):
        root, items = self.steam_root(), []
        if not root:
            return items
        for lib in self.steam_libraries(root):
            for mf in sorted(glob.glob(os.path.join(glob.escape(lib), "steamapps", "appmanifest_*.acf"))):
                st = parse_vdf(_read(mf)).get("AppState") or {}
                appid, name = st.get("appid", ""), st.get("name", "")
                path = os.path.join(lib, "steamapps", "common", st.get("installdir", ""))
                if not appid or not name or appid in STEAM_SKIP_IDS or STEAM_SKIP_NAMES.search(name):
                    continue
                if not int(st.get("StateFlags", "0") or 0) & 4 or not os.path.isdir(path):  # 4 = instalado por completo
                    continue
                cover, hero = self._steam_art(root, appid)
                items.append({"store": "steam", "store_id": appid, "name": name, "install_path": path,
                              "launch_type": "uri", "launch_uri": f"steam://rungameid/{appid}", "exe_path": "",
                              "size": fmt_size(st.get("SizeOnDisk")), "cover_src": cover, "hero_src": hero})
        return items

    def scan_gog(self):
        items = []
        for sub in self.reg.subkeys("HKLM", self.GOG_KEY):
            v = self.reg.values("HKLM", f"{self.GOG_KEY}\\{sub}") or {}
            name, path = v.get("gameName", ""), v.get("path", "")
            exe = v.get("launchCommand") or v.get("exe") or ""
            if exe and not ntpath.isabs(exe):
                exe = os.path.join(path, exe)
            if not name or v.get("dependsOn") or not exe or not os.path.exists(exe):  # DLC/extras ficam de fora
                continue
            items.append({"store": "gog", "store_id": str(v.get("gameID", sub)), "name": name, "install_path": path,
                          "launch_type": "direct", "launch_uri": "", "exe_path": exe, "size": "",
                          "cover_src": "", "hero_src": ""})
        return items

    def scan_epic(self):
        d, items = self.epic_dir(), []
        for f in sorted(glob.glob(os.path.join(glob.escape(d), "*.item"))) if d else []:
            try:
                m = json.loads(_read(f))
            except ValueError:
                continue
            cats = [str(c).lower() for c in m.get("AppCategories") or []]
            loc, name = m.get("InstallLocation", ""), m.get("DisplayName", "")
            if not name or m.get("bIsIncompleteInstall") or not os.path.isdir(loc) or (cats and "games" not in cats):
                continue
            ident = "%3A".join([m.get("CatalogNamespace", ""), m.get("CatalogItemId", ""), m.get("AppName", "")])
            items.append({"store": "epic", "store_id": m.get("AppName", ""), "name": name, "install_path": loc,
                          "launch_type": "uri", "exe_path": os.path.join(loc, m.get("LaunchExecutable", "")),
                          "launch_uri": f"com.epicgames.launcher://apps/{ident}?action=launch&silent=true",
                          "size": fmt_size(m.get("InstallSize")), "cover_src": "", "hero_src": ""})
        return items

    def scan_retrobat(self):
        """Jogos das pastas roms do RetroBat, com nome, sinopse e imagens das gamelist.xml."""
        rb, items = self.retrobat, []
        if not rb or not rb.root():
            return items
        for sm in rb.summary():
            for g in rb.games(sm["name"]):
                info = g["info"]
                year = (info.get("releasedate") or "")[:4]
                try:
                    rating = round(float(info.get("rating") or "") * 10, 1)  # gamelist: 0..1
                except ValueError:
                    rating = ""
                try:
                    size = fmt_size(os.path.getsize(g["rom"])) if os.path.isfile(g["rom"]) else ""
                except OSError:
                    size = ""
                items.append({"store": "retrobat", "store_id": f'{g["system"]}:{os.path.normcase(g["rom"])}',
                              "name": info.get("name") or clean_rom_name(g["name"]), "install_path": "",
                              "launch_type": "retrobat", "launch_uri": "",
                              "exe_path": "", "rom_path": g["rom"], "rb_system": g["system"],
                              "rb_system_name": g["system_name"], "size": size,
                              "cover_src": info.get("thumbnail") or info.get("boxart") or info.get("image") or "",
                              "hero_src": info.get("fanart") or "",
                              "description": info.get("desc", ""), "developer": info.get("developer", ""),
                              "genre": info.get("genre", ""), "release_date": year, "rating": rating,
                              "platform": g["system_name"]})
        return items

    def scan_folder(self, root):
        items = []
        try:
            entries = sorted((e for e in os.scandir(root) if e.is_dir()), key=lambda e: e.name.casefold())
        except OSError:
            return items
        for e in entries:
            exe = pick_exe(e.path)
            if exe:
                items.append({"store": "folder", "store_id": "", "name": clean_folder_name(e.name),
                              "install_path": e.path, "launch_type": "direct", "launch_uri": "", "exe_path": exe,
                              "size": "", "cover_src": "", "hero_src": ""})
        return items

    def scan(self, store, folder=None):
        fn = {"steam": self.scan_steam, "gog": self.scan_gog, "epic": self.scan_epic, "retrobat": self.scan_retrobat}
        if store == "all":
            items = [i for s in self.status() if s["found"] for i in fn[s["id"]]()]
        elif store == "folder":
            items = self.scan_folder(folder)
        else:
            items = fn[store]()
        for it in items:
            it["already"] = self._known(it)
        return items

    def _known(self, it):
        with self.games.lock:
            for g in self.games.catalog.values():
                if it["store_id"] and g.get("store") == it["store"] and g.get("store_id") == it["store_id"]:
                    return True
                if it.get("rom_path") and _norm(g.get("rom_path")) == _norm(it["rom_path"]):
                    return True
                if it["exe_path"] and _norm(g.get("exe_path")) == _norm(it["exe_path"]):
                    return True
                if it["install_path"] and _norm(g.get("install_path")) == _norm(it["install_path"]):
                    return True
        return False

    # -- importar
    def import_items(self, items):
        """Cria um jogo por item (ignora o que não passar na validação). Retorna (adicionados, ignorados)."""
        with self._import_lock:
            return self._import_items(items)

    def _import_items(self, items):
        added = skipped = 0
        gs = self.games
        for it in items:
            if self._known(it):  # já está na biblioteca (mesma loja/ID, mesma ROM, mesmo .exe): não duplica
                skipped += 1
                continue
            name = (it.get("name") or "").strip()
            lt = it.get("launch_type")
            ok = bool(name) and ((lt == "direct" and os.path.exists(it.get("exe_path", "")))
                                 or (lt == "retrobat" and self.retrobat is not None and os.path.exists(it.get("rom_path", ""))
                                     and bool(self.retrobat.system(it.get("rb_system", ""))))
                                 or (lt == "uri" and str(it.get("launch_uri", "")).startswith(ALLOWED_URI)))
            if not ok:
                skipped += 1
                continue
            with gs.lock:
                base, n = name, 2
                while name in gs.catalog:
                    name, n = f"{base} ({n})", n + 1
            safe = safe_name(name)
            entry = {"name": name, "description": "", "icon": None, "launch_type": lt, "source": "local",
                     "store": it.get("store", ""), "store_id": it.get("store_id", ""),
                     "install_path": it.get("install_path", ""),
                     "cover": gs._adopt_art(it.get("cover_src"), safe, "cover"),
                     "hero": gs._adopt_art(it.get("hero_src"), safe, "hero")}
            entry.update({k: "" for k in METADATA_KEYS})
            entry["size"] = it.get("size", "")
            if lt == "retrobat":
                entry.update(rom_path=it["rom_path"], rb_system=it["rb_system"], rb_system_name=it.get("rb_system_name", ""),
                             description=it.get("description", ""), platform=it.get("platform", ""),
                             developer=it.get("developer", ""), genre=it.get("genre", ""),
                             release_date=it.get("release_date", ""))
                if it.get("rating") != "":
                    entry["rating"] = it.get("rating")
            elif lt == "uri":
                entry["launch_uri"] = it["launch_uri"]
                if it.get("exe_path"):
                    entry["exe_path"] = it["exe_path"]
            else:
                entry["exe_path"] = it["exe_path"]
            with gs.lock:
                gs.catalog[name] = entry
            added += 1
        if added:
            from .store import save_json
            with gs.lock:
                save_json("catalog.json", gs.catalog)
        return added, skipped
