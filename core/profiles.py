"""Perfis locais do NeoSD (como os usuários do PS5): nome + foto, nível e troféus.

Não é uma conta: fica só neste PC (library/profiles.json e library/profiles/<id>.png).

Nível e troféus vêm das conquistas que este PC conhece:
  - Steam: cada conquista desbloqueada vira um troféu pela raridade global (como o PlayStation):
      bronze (mais de 50% dos jogadores têm), prata (20-50%), ouro (até 20%); sem raridade = bronze.
    Jogo da Steam com 100% = platina.
  - RetroAchievements: API_GetAchievementsEarnedBetween (todas as conquistas da conta, com os pontos): até 9 pontos =
    bronze, 10-24 = prata, 25 ou mais = ouro. Jogo 100% (API_GetUserCompletionProgress) = platina.
  XP por troféu (valores do PlayStation): bronze 15, prata 30, ouro 90, platina 300.

Cada conquista nova é creditada ao perfil que estava ATIVO quando o NeoSD a viu pela primeira vez. Na primeira
sincronização, tudo o que já existia vai para o primeiro perfil criado (o dono do PC).
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("neosd.profiles")
XP = {"bronze": 15, "silver": 30, "gold": 90, "platinum": 300}
GRADES = ("platinum", "gold", "silver", "bronze")
BUILTIN = re.compile(r"^(neo|emo)-[a-z0-9]+$")


def level_of(xp):
    """-> (nível, xp dentro do nível, xp para o próximo). Cada nível pede um pouco mais: 100, 140, 180…"""
    lvl, need, left = 1, 100, int(xp)
    while left >= need and lvl < 999:
        left -= need
        lvl += 1
        need = 100 + 40 * (lvl - 1)
    return lvl, left, need


def steam_grade(rarity):
    if rarity is None:
        return "bronze"
    return "gold" if rarity <= 20 else "silver" if rarity <= 50 else "bronze"


def ra_grade(points):
    p = int(points or 0)
    return "gold" if p >= 25 else "silver" if p >= 10 else "bronze"


class ProfileService:
    def __init__(self, data_dir, catalog, steam=None, ra=None, now=time.time):
        self.dir = Path(data_dir)
        self.catalog, self.steam, self.ra, self.now = catalog, steam, ra, now
        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self.notify = lambda data: None
        self.d = self._load()

    # ---------- arquivo ----------
    def _file(self):
        return self.dir / "profiles.json"

    def _load(self):
        try:
            d = json.loads(self._file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d.setdefault("profiles", [])
        d.setdefault("active", d["profiles"][0]["id"] if d["profiles"] else "")
        d.setdefault("known", {})     # chave da conquista -> id do perfil que recebeu
        d.setdefault("plat", {})      # jogo 100% -> id do perfil
        d.setdefault("ask", True)     # "Quem está jogando?" ao abrir o modo console
        d.setdefault("synced", 0)
        d.setdefault("ra_since", 0)
        return d

    def _save(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self._file().with_suffix(".tmp")
            tmp.write_text(json.dumps(self.d, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._file())
        except OSError:
            log.exception("salvar perfis")

    # ---------- perfis ----------
    def _find(self, pid):
        return next((p for p in self.d["profiles"] if p["id"] == pid), None)

    def active(self):
        return self._find(self.d["active"])

    def _stats(self, pid):
        tro = {g: 0 for g in GRADES}
        for key, owner in self.d["known"].items():
            if owner == pid:
                tro[key.split("|", 1)[0]] = tro.get(key.split("|", 1)[0], 0) + 1
        for owner in self.d["plat"].values():
            if owner == pid:
                tro["platinum"] += 1
        xp = sum(XP[g] * n for g, n in tro.items())
        lvl, cur, need = level_of(xp)
        return {"trophies": tro, "xp": xp, "level": lvl, "lvl_xp": cur, "lvl_need": need,
                "total": sum(tro.values())}

    def avatar_url(self, p):
        a = p.get("avatar") or "neo-roxo"
        if a == "file":
            f = self.dir / "profiles" / f"{p['id']}.png"
            return _data_uri(f) if f.is_file() else ""
        return a

    def to_ui(self, p):
        return {"id": p["id"], "name": p["name"], "avatar": self.avatar_url(p), "created": p.get("created", 0),
                **self._stats(p["id"])}

    def state(self):
        with self._lock:
            ps = [self.to_ui(p) for p in self.d["profiles"]]
        return {"profiles": ps, "active": self.d["active"], "ask": bool(self.d["ask"]), "synced": self.d["synced"],
                "steam": bool(self.steam and self.steam.stats_dir()), "ra": bool(self.ra and self.ra.configured())}

    def save(self, data):
        """Cria ou edita: {id?, name, avatar?}. avatar = 'neo-…'/'emo-…' (prontos) ou 'file' (foto escolhida)."""
        name = re.sub(r"\s+", " ", str(data.get("name") or "")).strip()[:24]
        if not name:
            return False, "Escolha um nome.", None
        av = str(data.get("avatar") or "")
        with self._lock:
            p = self._find(data.get("id") or "")
            first = not self.d["profiles"]
            if not p:
                p = {"id": uuid.uuid4().hex[:10], "name": name, "avatar": "neo-roxo", "created": int(self.now())}
                self.d["profiles"].append(p)
                if first:
                    self.d["active"] = p["id"]
                    for k in self.d["known"]:  # conquistas vistas antes de existir perfil: do dono do PC
                        self.d["known"][k] = p["id"]
                    for k in self.d["plat"]:
                        self.d["plat"][k] = p["id"]
            p["name"] = name
            if BUILTIN.match(av) or (av == "file" and (self.dir / "profiles" / f"{p['id']}.png").is_file()):
                p["avatar"] = av
            self._save()
            return True, "", p["id"]

    def set_photo(self, pid, path):
        """Copia a foto escolhida (recorte quadrado no centro, 256 px) para library/profiles/<id>.png."""
        from PIL import Image, ImageOps
        p = self._find(pid)
        if not p:
            return False, "Perfil não encontrado."
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im).convert("RGBA")
                im = ImageOps.fit(im, (256, 256), Image.LANCZOS)
                (self.dir / "profiles").mkdir(parents=True, exist_ok=True)
                im.save(self.dir / "profiles" / f"{pid}.png", "PNG")
        except Exception as e:
            return False, f"Não consegui usar essa imagem ({e})."
        with self._lock:
            p["avatar"] = "file"
            self._save()
        _URI.pop(str(self.dir / "profiles" / f"{pid}.png"), None)
        return True, ""

    def delete(self, pid):
        with self._lock:
            p = self._find(pid)
            if not p:
                return False
            self.d["profiles"].remove(p)
            heir = self.d["profiles"][0]["id"] if self.d["profiles"] else ""
            for tbl in ("known", "plat"):  # os troféus não somem: ficam com o primeiro perfil
                for k, v in self.d[tbl].items():
                    if v == pid:
                        self.d[tbl][k] = heir
            if self.d["active"] == pid:
                self.d["active"] = heir
            self._save()
        try:
            (self.dir / "profiles" / f"{pid}.png").unlink(missing_ok=True)
        except OSError:
            pass
        return True

    def select(self, pid):
        with self._lock:
            if not self._find(pid):
                return False
            self.d["active"] = pid
            self._save()
        return True

    def set_ask(self, on):
        with self._lock:
            self.d["ask"] = bool(on)
            self._save()

    # ---------- troféus ----------
    def _steam_items(self):
        """{chave: grade} e jogos 100% da Steam (só os jogos da biblioteca do NeoSD)."""
        out, plats = {}, []
        if not (self.steam and self.steam.stats_dir()):
            return out, plats
        apps = {str(g.get("store_id")) for g in list(self.catalog.values()) if g.get("store") == "steam" and g.get("store_id")}
        for appid in apps:
            try:
                r = self.steam.game(appid, rarity=True)
            except Exception:
                continue
            if not r.get("ok"):
                continue
            for a in r["items"]:
                if a["unlocked"]:
                    out[f"steam:{appid}:{a['key']}"] = steam_grade(a.get("rarity"))
            if r["total"] and r["unlocked"] == r["total"]:
                plats.append(f"steam:{appid}")
        return out, plats

    def _ra_items(self):
        out, plats = {}, []
        if not (self.ra and self.ra.configured()):
            return out, plats
        since = int(self.d.get("ra_since") or 0)
        start = since - 86400 if since else 1325376000  # 2012: antes do RA existir
        now = int(self.now())
        try:
            got = self.ra._api("GetAchievementsEarnedBetween", f=start, t=now)
            for a in got if isinstance(got, list) else []:
                if a.get("AchievementID"):
                    out[f"ra:{a['AchievementID']}"] = ra_grade(a.get("Points"))
            prog = self.ra._api("GetUserCompletionProgress", c=500)
            for g in (prog.get("Results") or []) if isinstance(prog, dict) else []:
                total, have = int(g.get("MaxPossible") or 0), int(g.get("NumAwarded") or 0)
                if total and have >= total:
                    plats.append(f"ra:{g.get('GameID')}")
            self.d["ra_since"] = now
        except Exception as e:
            log.info("RA troféus: %s", e)
        return out, plats

    def sync(self):
        """Lê as conquistas e credita as novas ao perfil ativo. -> quantas novas."""
        if not self._sync_lock.acquire(blocking=False):
            return 0
        try:
            st, sp = self._steam_items()
            rt, rp = self._ra_items()
            new = 0
            with self._lock:
                owner = self.d["active"]  # sem perfil ainda: guarda com dono vazio e passa ao primeiro criado
                known = self.d["known"]
                have = {k.split("|", 1)[1] for k in known}
                for key, grade in {**st, **rt}.items():
                    if key not in have:
                        known[f"{grade}|{key}"] = owner
                        have.add(key)
                        new += 1
                for key in sp + rp:
                    if key not in self.d["plat"]:
                        self.d["plat"][key] = owner
                        new += 1
                self.d["synced"] = int(self.now())
                self._save()
            if new:
                log.info("troféus: %d novo(s) para %s", new, (self.active() or {}).get("name", "(sem perfil)"))
            self.notify(self.state())
            return new
        finally:
            self._sync_lock.release()

    def sync_soon(self, delay=3):
        threading.Timer(delay, self.sync).start()


_URI = {}


def _data_uri(path):
    import base64
    p = str(path)
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return ""
    hit = _URI.get(p)
    if hit and hit[0] == mt:
        return hit[1]
    with open(p, "rb") as f:
        uri = "data:image/png;base64," + base64.b64encode(f.read()).decode()
    _URI[p] = (mt, uri)
    return uri
