"""Orquestra o scraper: configuração, busca multi-fonte com pontuação, pré-visualização (com
enriquecimento entre fontes), aplicação das imagens/dados e o modo automático/em lote."""
import copy
import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageOps

from ..games import safe_name
from ..store import LIBRARY, load_json, resolve, save_json
from .base import Http, ScraperError, norm_title, parse_year, score_candidate
from .platforms import detect_platforms
from .providers import IGDB, ScreenScraper, Steam, SteamGridDB

CONFIG = "library/scraper_config.json"
MASK = "••••••••"
DEFAULTS = {
    "language": "pt", "auto_on_insert": False, "steam": {"enabled": True},
    "igdb": {"enabled": True, "client_id": "", "client_secret": "", "access_token": "", "token_expires_at": 0},
    "steamgriddb": {"enabled": True, "api_key": ""},
    "screenscraper": {"enabled": True, "devid": "", "devpassword": "", "username": "", "password": ""},
}
FIELDS = (("description", "Sinopse"), ("genre", "Gênero"), ("developer", "Desenvolvedora"),
          ("release_date", "Lançamento"), ("rating", "Nota"))
CAND_KEYS = {"description": "description", "genre": "genres", "developer": "developer",
             "release_date": "date", "rating": "rating"}
ART_LABEL = {"cover": "Capa", "icon": "Ícone", "hero": "Banner"}


class ScraperService:
    def __init__(self, games, http=None):
        self.games, self._http = games, http
        self.cfg = copy.deepcopy(DEFAULTS)
        raw = load_json(CONFIG)
        for k in ("language", "auto_on_insert"):
            if k in raw:
                self.cfg[k] = raw[k]
        for sec in ("steam", "igdb", "steamgriddb", "screenscraper"):
            self.cfg[sec].update(raw.get(sec) or {})
        self._pending, self._cancel, self._thread = [], threading.Event(), None
        self._build()

    # ---------- infra ----------
    @property
    def http(self):
        if self._http is None:
            self._http = Http()
        return self._http

    def _build(self):
        mk = lambda cls, sec: cls(self.http if self._http else _LazyHttp(self), self.cfg[sec], self._persist)
        self.steam, self.igdb = mk(Steam, "steam"), mk(IGDB, "igdb")
        self.ss, self.sgdb = mk(ScreenScraper, "screenscraper"), mk(SteamGridDB, "steamgriddb")
        self.meta = [self.steam, self.igdb, self.ss]
        self.names = {p.id: p.name for p in self.meta + [self.sgdb]}

    def _persist(self):
        save_json(CONFIG, self.cfg)

    # ---------- configuração ----------
    def get_config(self):
        provs = []
        for p in self.meta + [self.sgdb]:
            provs.append({"id": p.id, "name": p.name, "kind": p.kind, "needs_account": p.needs_account,
                          "configured": p.configured(), "enabled": p.enabled, "help": p.help,
                          "fields": [{"key": k, "label": l, "secret": s, "required": r,
                                      "value": "" if s else self.cfg[p.id].get(k, ""),
                                      "set": bool(self.cfg[p.id].get(k))} for k, l, s, r in p.fields]})
        return {"language": self.cfg["language"], "auto_on_insert": bool(self.cfg["auto_on_insert"]), "providers": provs}

    def save_config(self, data):
        """Parcial: só o que veio. Campos secretos com a máscara ficam como estão."""
        for k in ("language", "auto_on_insert"):
            if k in data:
                self.cfg[k] = data[k] if k != "language" else (data[k] if data[k] in ("pt", "en") else "pt")
        for sec in ("steam", "igdb", "steamgriddb", "screenscraper"):
            for key, val in (data.get(sec) or {}).items():
                if key == "enabled":
                    self.cfg[sec]["enabled"] = bool(val)
                elif key in self.cfg[sec] and isinstance(val, str) and val != MASK:
                    self.cfg[sec][key] = val.strip()
                    if sec == "igdb":  # credencial nova -> token antigo não vale mais
                        self.cfg["igdb"].update(access_token="", token_expires_at=0)
        self._persist()

    def test(self, provider_id):
        p = next((x for x in self.meta + [self.sgdb] if x.id == provider_id), None)
        if not p:
            return False, "Fonte desconhecida."
        if not p.configured():
            return False, "Preencha as credenciais primeiro."
        try:
            return True, p.test()
        except ScraperError as e:
            return False, str(e)

    # ---------- dicas de busca ----------
    def hints_for(self, g):
        emu = self.games.emulators.get(g.get("emulator_id", "")) if g.get("launch_type") == "emulator" else None
        keys = detect_platforms(g.get("platform", ""), (emu or {}).get("name", ""))
        if not keys and not emu:
            keys = ["pc"]
        from ..games import clean_rom_name
        return {"query": clean_rom_name(g.get("name", "")), "platforms": keys, "year": parse_year(g.get("release_date")),
                "rom": os.path.basename(g.get("rom_path", "")) if emu else "",
                "steam_id": g.get("store_id", "") if g.get("store") == "steam" else ""}

    def _game(self, gid):
        g = self.games.catalog.get(gid)
        if not g:
            raise ScraperError("Jogo não encontrado.")
        return g

    # ---------- busca ----------
    def _search(self, q, hints, exclude=None):
        provs = [p for p in self.meta if p.id != exclude and p.enabled and p.configured() and p.applies(hints)]
        if not provs:
            return [], ["Nenhuma fonte disponível para este jogo (configure uma na aba Scraper)."]

        def run(p):
            try:
                return p.search(q, hints), None
            except ScraperError as e:
                return [], f"{p.name}: {e}"
        with ThreadPoolExecutor(len(provs)) as ex:
            results = list(ex.map(run, provs))
        cands, warnings = [], []
        for found, warn in results:
            cands += found
            if warn:
                warnings.append(warn)
        for c in cands:
            score_candidate(c, q, hints)
        lang = self.cfg["language"]  # empate de nota: vence quem fala o idioma preferido (ex.: Steam em pt)
        cands.sort(key=lambda c: (-c["score"], not c["rom_match"], c["desc_lang"] != lang, c["provider"]))
        return cands, warnings

    def search(self, game_id, query=None):
        g = self._game(game_id)
        hints = self.hints_for(g)
        q = (query or "").strip() or hints["query"]
        cands, warnings = self._search(q, hints)
        return {"query": q, "candidates": cands[:8], "warnings": warnings}

    # ---------- pré-visualização ----------
    def _enrich(self, meta, hints):
        """Completa o que a fonte escolhida não trouxe (e a sinopse no idioma preferido) com as outras."""
        lang = self.cfg["language"]
        missing = lambda: [k for k in ("description", "genres", "developer", "date", "rating") if not meta.get(k)]
        wrong_lang = lambda: bool(meta["description"] and meta["desc_lang"] and meta["desc_lang"] != lang)
        extras, sources = [], [self.names[meta["provider"]]]
        h = {**hints, "year": meta.get("year") or hints.get("year"), "platforms": meta.get("platform_keys") or hints["platforms"]}
        for p in self.meta:
            if not (missing() or wrong_lang()):
                break
            if p.id == meta["provider"] or not (p.enabled and p.configured() and p.applies(h)):
                continue
            try:
                found = p.search(meta["title"], h)
            except ScraperError:
                continue
            for c in found:
                score_candidate(c, meta["title"], h)
            ok = [c for c in found if c["exact"] and c["score"] >= 0.8
                  and not (h["year"] and c["year"] and abs(h["year"] - c["year"]) > 1)]
            if not ok:
                continue
            best = max(ok, key=lambda c: c["score"])
            for k in missing():
                if best.get(k):
                    meta[k] = best[k]
            if best["description"] and best["desc_lang"] == lang and meta["desc_lang"] != lang:
                meta["description"], meta["desc_lang"] = best["description"], lang
                if best["genres"]:  # gêneros vêm traduzidos junto com a sinopse
                    meta["genres"] = best["genres"]
            extras.append(best)
            sources.append(p.name)
        return extras, sources

    def _art(self, meta, extras):
        steam_id = next((c["ref"] for c in [meta] + extras if c["provider"] == "steam"), None)
        sg = {"cover": [], "icon": [], "hero": []}
        if self.sgdb.enabled and self.sgdb.configured():
            try:
                sg = self.sgdb.art(meta["title"], steam_id)
            except ScraperError:
                pass
        opts, seen = {"cover": [], "icon": [], "hero": []}, set()

        def add(kind, url, thumb="", source=""):
            if url and url not in seen and len(opts[kind]) < 6:
                seen.add(url)
                opts[kind].append({"url": url, "thumb": thumb or url, "source": source})
        for kind in ("cover", "hero"):
            add(kind, meta[kind], "", self.names[meta["provider"]])
            for o in sg[kind]:
                add(kind, o["url"], o["thumb"], o["source"])
            for c in extras:
                add(kind, c[kind], "", self.names[c["provider"]])
        for o in sg["icon"]:
            add("icon", o["url"], o["thumb"], o["source"])
        for c in [meta] + extras:
            add("icon", c["icon"], "", self.names[c["provider"]])
        opts["icon"].append({"url": "@cover", "thumb": "", "source": "recortar da capa"})
        return opts

    def preview(self, game_id, cand):
        g = self._game(game_id)
        meta = dict(cand)
        extras, sources = self._enrich(meta, self.hints_for(g))
        opts = self._art(meta, extras)
        fields = []
        for key, label in FIELDS:
            val = meta.get(CAND_KEYS[key])
            val = ", ".join(val[:3]) if isinstance(val, list) else val
            if val in (None, "", 0):
                continue
            cur = g.get(key)
            fields.append({"key": key, "label": label, "new": val, "current": "" if cur in (None, "") else str(cur),
                           "default_on": cur in (None, "")})
        has = {k: bool((p := resolve(g.get(k))) and p.is_file()) for k in ("cover", "icon", "hero")}
        art = {k: {"options": opts[k], "current": has[k], "default_on": not has[k] and bool(opts[k])} for k in opts}
        return {"title": meta["title"], "year": meta.get("year"), "sources": sources, "fields": fields, "art": art,
                "meta": {"provider": meta["provider"], "title": meta["title"], "ref": meta["ref"]}}

    def default_selection(self, pv):
        sel = {"fields": {f["key"]: f["new"] for f in pv["fields"] if f["default_on"]}, "art": {}, "meta": pv["meta"]}
        for k, a in pv["art"].items():
            if a["default_on"] and a["options"]:
                sel["art"][k] = a["options"][0]["url"]
        return sel

    # ---------- aplicar ----------
    @staticmethod
    def _prep(data, kind):
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            buf = io.BytesIO()
            if kind == "icon":
                im = ImageOps.fit(im.convert("RGBA"), (256, 256), centering=(0.5, 0.3))
                im.save(buf, "PNG")
                return buf.getvalue(), "icon.png"
            im = im.convert("RGB")
            im.thumbnail((900, 1350) if kind == "cover" else (1920, 1080))
            im.save(buf, "JPEG", quality=88 if kind == "cover" else 82)
            return buf.getvalue(), f"{kind}.jpg"

    def apply(self, game_id, sel):
        g = self._game(game_id)
        changes, applied, failed = {}, [], []
        labels = dict(FIELDS)
        for key, val in (sel.get("fields") or {}).items():
            if key not in labels:
                continue
            if key == "rating":
                try:
                    r = float(val)
                except (TypeError, ValueError):
                    continue
                if 0 <= r <= 10:
                    changes["rating"] = round(r, 1)
                    applied.append(labels[key])
            elif str(val).strip():
                changes[key] = str(val).strip()
                applied.append(labels[key])
        safe, folder, cover_bytes = safe_name(g.get("name", game_id)), None, None
        folder = LIBRARY / safe
        for kind in ("cover", "hero", "icon"):
            url = (sel.get("art") or {}).get(kind)
            if not url:
                continue
            try:
                if url == "@cover":
                    cur = resolve(g.get("cover"))
                    data = cover_bytes or (cur.read_bytes() if cur and cur.is_file() else None)
                    if not data:
                        raise ScraperError("não há capa para recortar")
                else:
                    data = self.http.download(url)
                if kind == "cover":
                    cover_bytes = data
                payload, fname = self._prep(data, kind)
                folder.mkdir(parents=True, exist_ok=True)
                (folder / fname).write_bytes(payload)
                changes[kind] = f"library/{safe}/{fname}"
                applied.append(ART_LABEL[kind])
            except ScraperError as e:
                failed.append(f"{ART_LABEL[kind]}: {e}")
            except Exception:
                failed.append(f"{ART_LABEL[kind]}: imagem inválida")
        meta = sel.get("meta") or {}
        changes["scrape"] = {"provider": meta.get("provider", ""), "title": meta.get("title", ""),
                             "ref": meta.get("ref", ""), "at": int(time.time())}
        self.games.update_game(game_id, changes, remove=("scrape_miss",))
        self._pending = [p for p in self._pending if p["id"] != game_id]
        return {"ok": bool(applied) or not failed, "applied": applied, "failed": failed}

    # ---------- automático / lote ----------
    def auto(self, game_id):
        """Busca e aplica sozinho SÓ quando o acerto é inequívoco; senão deixa pra revisão."""
        g = self._game(game_id)
        res = self.search(game_id)
        cands = res["candidates"]
        if not cands:
            if res["warnings"]:
                raise ScraperError(" · ".join(res["warnings"]))
            self.games.update_game(game_id, {"scrape_miss": int(time.time())})
            return {"status": "notfound", "name": g["name"]}
        best = cands[0]
        group = lambda c: (norm_title(c["title"]), c["year"])
        other = next((c for c in cands[1:] if group(c) != group(best)), None)
        if best.get("rom_match") or (best["exact"] and best["score"] >= 0.85
                                     and (not other or best["score"] - other["score"] >= 0.1)):
            r = self.apply(game_id, self.default_selection(self.preview(game_id, best)))
            return {"status": "applied", "name": g["name"], **r}
        if not any(p["id"] == game_id for p in self._pending):
            self._pending.append({"id": game_id, "name": g["name"]})
        return {"status": "review", "name": g["name"]}

    def needs(self, g):
        cover = resolve(g.get("cover"))
        if g.get("scrape_miss") and time.time() - g["scrape_miss"] < 30 * 86400:
            return False
        return not (cover and cover.is_file()) or not g.get("description")

    def pending(self):
        return list(self._pending)

    def available(self):
        return any(p.enabled and p.configured() for p in self.meta)

    def batch_running(self):
        return bool(self._thread and self._thread.is_alive())

    def batch_cancel(self):
        self._cancel.set()

    def batch_start(self, notify):
        if self.batch_running():
            return False, "Já existe uma busca em andamento.", 0
        if not self.available():
            return False, "Nenhuma fonte configurada.", 0
        ids = [k for k, g in list(self.games.catalog.items()) if self.needs(g)]
        if not ids:
            return False, "Todos os jogos já têm capa e sinopse.", 0
        self._cancel.clear()
        self._pending = []
        self._thread = threading.Thread(target=self._run_batch, args=(ids, notify), daemon=True)
        self._thread.start()
        return True, "", len(ids)

    def _run_batch(self, ids, notify):
        st = {"applied": 0, "review": 0, "notfound": 0, "failed": 0}
        errors_in_row, last_error = 0, ""
        for i, gid in enumerate(ids):
            if self._cancel.is_set():
                break
            notify({"done": i, "total": len(ids), "current": gid, "finished": False, **st})
            try:
                r = self.auto(gid)
                st[r["status"] if r["status"] in st else "failed"] += 1
                errors_in_row = 0
            except ScraperError as e:
                st["failed"] += 1
                errors_in_row, last_error = errors_in_row + 1, str(e)
                if errors_in_row >= 3:  # provável falta de internet/credencial ruim: não adianta insistir
                    break
        notify({"done": len(ids) if errors_in_row < 3 and not self._cancel.is_set() else i, "total": len(ids),
                "current": "", "finished": True, "cancelled": self._cancel.is_set(),
                "error": last_error if errors_in_row >= 3 else "", "pending": self.pending(), **st})


class _LazyHttp:
    """Só cria o Http (que exige `requests`) quando a primeira chamada de rede acontece."""
    def __init__(self, svc):
        self._svc = svc

    def __getattr__(self, name):
        return getattr(self._svc.http, name)
