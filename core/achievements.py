"""Conquistas da Steam lidas dos arquivos que a própria Steam guarda no PC (sem chave da Web API e sem login).

Onde ficam (pasta da Steam):
  appcache/stats/UserGameStatsSchema_<appid>.bin  -> lista de conquistas do jogo: nome e descrição em vários
      idiomas, secreta ou não, ícones e, em alguns jogos, a estatística que mede o progresso;
  appcache/stats/UserGameStats_<conta>_<appid>.bin -> o que a conta desbloqueou: em cada grupo, "data" é um
      número em que cada bit é uma conquista e "AchievementTimes" guarda a data (Unix) de cada desbloqueio.
Os dois estão no formato binário de KeyValues da Valve. A Steam cria/atualiza esses arquivos quando o jogo roda
ou quando a página do jogo é aberta na Steam; um jogo nunca aberto neste PC ainda não tem os arquivos.

A raridade (porcentagem global) vem de GetGlobalAchievementPercentagesForApp, que é público e não pede chave.
"""
import json
import logging
import os
import re
import struct
import threading
import time
from pathlib import Path

log = logging.getLogger("neosd.achievements")
STEAM64_BASE = 76561197960265728
ICON_URL = "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon}"
RARITY_URL = "https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/"
LANGS = ("brazilian", "portuguese", "english")
RARITY_TTL, RARITY_FAIL_TTL = 3 * 86400, 3600
USER_RE = re.compile(r"^UserGameStats_(\d+)_(\d+)\.bin$")


# ---------- formato binário de KeyValues ----------
def parse_kv(b, i=0, depth=0):
    """-> (dict, posição). Tipos: 0 mapa, 1 texto, 2 int32, 3 float, 4 ponteiro, 5 texto largo, 6 cor,
    7 uint64, 8/11 fim do mapa, 10 int64."""
    if depth > 64:
        raise ValueError("KeyValues aninhado demais")
    out = {}
    n = len(b)
    while i < n:
        t = b[i]
        i += 1
        if t in (8, 11):
            return out, i
        j = b.index(0, i)
        key = b[i:j].decode("utf-8", "replace")
        i = j + 1
        if t == 0:
            val, i = parse_kv(b, i, depth + 1)
        elif t == 1:
            j = b.index(0, i)
            val = b[i:j].decode("utf-8", "replace")
            i = j + 1
        elif t == 5:
            j = i
            while b[j:j + 2] != b"\0\0":
                j += 2
            val = b[i:j].decode("utf-16-le", "replace")
            i = j + 2
        elif t in (2, 4, 6):
            val = struct.unpack_from("<i", b, i)[0]
            i += 4
        elif t == 3:
            val = struct.unpack_from("<f", b, i)[0]
            i += 4
        elif t == 7:
            val = struct.unpack_from("<Q", b, i)[0]
            i += 8
        elif t == 10:
            val = struct.unpack_from("<q", b, i)[0]
            i += 8
        else:
            raise ValueError(f"tipo {t} desconhecido na posição {i}")
        out[key] = val
    return out, i


def read_kv(path):
    try:
        return parse_kv(Path(path).read_bytes())[0]
    except (OSError, ValueError, IndexError, struct.error) as e:
        log.warning("não consegui ler %s: %s", path, e)
        return None


def _text(v):
    """Texto no idioma do usuário (português do Brasil, depois português, inglês ou o que houver)."""
    if isinstance(v, str):
        return v
    if not isinstance(v, dict):
        return ""
    for lang in LANGS:
        if v.get(lang):
            return str(v[lang])
    return next((str(x) for k, x in v.items() if k != "token" and isinstance(x, str) and x), "")


def _is_ach(stat):
    return str(stat.get("type")) in ("ACHIEVEMENTS", "4") and isinstance(stat.get("bits"), dict)


class SteamAchievements:
    def __init__(self, steam_root, data_dir, http=None, now=time.time):
        self._root = steam_root  # função que devolve a pasta da Steam (ou "")
        self.data_dir, self._http, self.now = Path(data_dir), http, now
        self._schemas = {}  # appid -> (mtime, dados)
        self._acc = (0.0, None)
        self._seen = None  # appid -> conjunto de conquistas desbloqueadas (para avisar as novas)
        self._mtimes = {}
        self._lock = threading.Lock()
        self._rarity = None

    # ---------- onde estão os arquivos ----------
    def stats_dir(self):
        root = self._root() if callable(self._root) else self._root
        p = Path(root) / "appcache" / "stats" if root else None
        return p if p and p.is_dir() else None

    def account(self):
        """Conta da Steam usada por último neste PC (número curto do nome dos arquivos)."""
        t, acc = self._acc
        if acc and self.now() - t < 60:
            return acc
        acc = self._account_from_login() or self._account_from_files()
        self._acc = (self.now(), acc)
        return acc

    def _account_from_login(self):
        root = self._root() if callable(self._root) else self._root
        if not root:
            return None
        try:
            from .libraries import parse_vdf
            text = (Path(root) / "config" / "loginusers.vdf").read_text(encoding="utf-8", errors="replace")
            users = (parse_vdf(text) or {}).get("users") or {}
        except Exception:
            return None
        best = None
        for sid, u in users.items():
            if not (str(sid).isdigit() and isinstance(u, dict)):
                continue
            score = (str(u.get("MostRecent", u.get("mostrecent", "0"))) == "1", int(u.get("Timestamp", 0) or 0))
            if best is None or score > best[0]:
                best = (score, int(sid) - STEAM64_BASE)
        return str(best[1]) if best and best[1] > 0 else None

    def _account_from_files(self):
        d = self.stats_dir()
        if not d:
            return None
        newest = {}
        for f in d.iterdir():
            m = USER_RE.match(f.name)
            if m:
                newest[m.group(1)] = max(newest.get(m.group(1), 0), f.stat().st_mtime)
        return max(newest, key=newest.get) if newest else None

    # ---------- dados de um jogo ----------
    def schema(self, appid):
        d = self.stats_dir()
        f = d / f"UserGameStatsSchema_{appid}.bin" if d else None
        if not f or not f.is_file():
            return None
        mt = f.stat().st_mtime
        hit = self._schemas.get(appid)
        if hit and hit[0] == mt:
            return hit[1]
        kv = read_kv(f)
        if kv is None:
            return None
        app = kv.get(str(appid)) or next((v for v in kv.values() if isinstance(v, dict)), {})
        stats = app.get("stats") or {}
        statname = {str(s.get("name")): sid for sid, s in stats.items() if isinstance(s, dict) and s.get("name")}
        items = []
        for sid, st in sorted(stats.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
            if not (isinstance(st, dict) and _is_ach(st)):
                continue
            for bit, a in sorted(st["bits"].items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
                if not isinstance(a, dict):
                    continue
                disp = a.get("display") or {}
                prog = a.get("progress") or {}
                op = (prog.get("value") or {}) if isinstance(prog, dict) else {}
                pstat = statname.get(str(op.get("operand1", ""))) if op.get("operation") == "statvalue" else None
                items.append({
                    "key": f"{sid}:{bit}", "sid": sid, "bit": int(bit), "api": str(a.get("name", "")),
                    "name": _text(disp.get("name")) or str(a.get("name", "")), "desc": _text(disp.get("desc")),
                    "hidden": str(disp.get("hidden", "0")) == "1",
                    "icon": str(disp.get("icon") or ""), "icon_gray": str(disp.get("icon_gray") or ""),
                    "prog": {"sid": pstat, "min": float(prog.get("min_val", 0) or 0),
                             "max": float(prog.get("max_val", 0) or 0)} if pstat else None})
        data = {"name": str(app.get("gamename") or ""), "items": items}
        self._schemas[appid] = (mt, data)
        return data

    def user(self, appid):
        """-> ({chave: data do desbloqueio (0 se a Steam não guardou)}, {id da estatística: valor}, mtime)."""
        d, acc = self.stats_dir(), self.account()
        f = d / f"UserGameStats_{acc}_{appid}.bin" if d and acc else None
        if not f or not f.is_file():
            return None
        kv = read_kv(f)
        if kv is None:
            return None
        cache = kv.get("cache") or {}
        unlocked, values = {}, {}
        for sid, st in cache.items():
            if not isinstance(st, dict):
                continue
            data = st.get("data", 0)
            values[sid] = data
            times = st.get("AchievementTimes") if isinstance(st.get("AchievementTimes"), dict) else {}
            if isinstance(data, int):
                bits = data & 0xFFFFFFFF
                for b in range(32):
                    if bits >> b & 1:
                        unlocked[f"{sid}:{b}"] = int(times.get(str(b), 0) or 0)
        return unlocked, values, f.stat().st_mtime

    def summary(self, appid):
        sc = self.schema(appid)
        if not sc or not sc["items"]:
            return {"ok": False, "total": 0, "unlocked": 0}
        u = self.user(appid)
        keys = {i["key"] for i in sc["items"]}
        n = len(keys & set(u[0])) if u else 0
        return {"ok": True, "total": len(keys), "unlocked": n, "synced": bool(u)}

    def game(self, appid, rarity=True):
        appid = str(appid)
        if not self.stats_dir():
            return {"ok": False, "reason": "Steam não encontrada neste PC."}
        sc = self.schema(appid)
        if sc is None:
            return {"ok": False, "reason": "A Steam ainda não baixou as conquistas deste jogo. "
                                           "Abra o jogo (ou a página dele na Steam) uma vez e volte aqui."}
        if not sc["items"]:
            return {"ok": False, "reason": "Este jogo não tem conquistas na Steam."}
        u = self.user(appid)
        unlocked, values, mt = u if u else ({}, {}, 0)
        pct = self.rarity(appid) if rarity else {}
        out = []
        for a in sc["items"]:
            got = a["key"] in unlocked
            item = {"key": a["key"], "name": a["name"], "desc": a["desc"], "hidden": a["hidden"], "unlocked": got,
                    "time": unlocked.get(a["key"], 0),
                    "icon": ICON_URL.format(appid=appid, icon=a["icon"] if got or not a["icon_gray"] else a["icon_gray"])
                    if (a["icon"] or a["icon_gray"]) else "",
                    "rarity": pct.get(a["api"])}
            p = a["prog"]
            if p and not got and p["max"] > p["min"]:
                cur = values.get(p["sid"], 0)
                if isinstance(cur, (int, float)) and cur > p["min"]:
                    item["progress"] = [min(cur, p["max"]), p["max"]]
            out.append(item)
        n = sum(1 for i in out if i["unlocked"])
        return {"ok": True, "appid": appid, "total": len(out), "unlocked": n, "synced": bool(u), "updated": mt,
                "items": out}

    # ---------- raridade global ----------
    def _rarity_file(self):
        return self.data_dir / "achievements_rarity.json"

    def rarity(self, appid):
        """Porcentagem de jogadores que têm cada conquista (guardada por 3 dias; sem internet, fica vazio)."""
        with self._lock:
            if self._rarity is None:
                try:
                    self._rarity = json.loads(self._rarity_file().read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    self._rarity = {}
            hit = self._rarity.get(appid)
            if hit and self.now() - hit.get("t", 0) < (RARITY_TTL if hit.get("p") else RARITY_FAIL_TTL):
                return hit.get("p") or {}
        pct = {}
        try:
            http = self._http
            if http is None:
                import requests as http
            r = http.get(RARITY_URL, params={"gameid": appid}, timeout=6)
            r.raise_for_status()
            for a in ((r.json().get("achievementpercentages") or {}).get("achievements") or []):
                try:
                    pct[str(a["name"])] = round(float(a["percent"]), 1)
                except (KeyError, TypeError, ValueError):
                    pass
        except Exception as e:
            log.info("raridade das conquistas de %s indisponível: %s", appid, e)
        with self._lock:
            self._rarity[appid] = {"t": self.now(), "p": pct}
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                tmp = self._rarity_file().with_suffix(".tmp")
                tmp.write_text(json.dumps(self._rarity), encoding="utf-8")
                os.replace(tmp, self._rarity_file())
            except OSError:
                pass
        return pct

    # ---------- avisar conquistas novas ----------
    def poll(self):
        """Compara os arquivos de desbloqueio com a última olhada. -> [(appid, item do esquema, data)].
        Na primeira chamada só memoriza o estado atual (não avisa o que já estava desbloqueado)."""
        d, acc = self.stats_dir(), self.account()
        if not d or not acc:
            return []
        first = self._seen is None
        if first:
            self._seen = {}
        new = []
        for f in d.glob(f"UserGameStats_{acc}_*.bin"):
            m = USER_RE.match(f.name)
            if not m:
                continue
            appid = m.group(2)
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if self._mtimes.get(appid) == mt:
                continue
            self._mtimes[appid] = mt
            u = self.user(appid)
            if not u:
                continue
            now = set(u[0])
            before = self._seen.get(appid)
            self._seen[appid] = now
            if first or before is None:
                continue
            gained = now - before
            if gained:
                sc = self.schema(appid) or {"items": []}
                by = {i["key"]: i for i in sc["items"]}
                for k in sorted(gained, key=lambda k: u[0].get(k, 0)):
                    if k in by:
                        new.append((appid, by[k], u[0].get(k, 0)))
        return new
