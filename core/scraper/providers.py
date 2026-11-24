"""Fontes de dados. Cada uma devolve candidatos no mesmo formato (ver base.new_candidate).
Steam / IGDB / ScreenScraper = metadados + arte; SteamGridDB = só arte (a melhor, da comunidade)."""
import time
from datetime import datetime, timezone

from .base import ScraperError, clean_text, fmt_date, new_candidate, parse_year, score_candidate
from .platforms import igdb_id, key_from_igdb, key_from_ss, ss_id


class Provider:
    id = name = help = ""
    kind = "meta"
    needs_account = True
    fields = ()  # (chave, rótulo, secreto?, obrigatório?)

    def __init__(self, http, cfg, persist=lambda: None):
        self.http, self.cfg, self.persist = http, cfg, persist

    @property
    def enabled(self):
        return self.cfg.get("enabled", True)

    def configured(self):
        return all(self.cfg.get(k) for k, _, _, req in self.fields if req)

    def applies(self, hints):
        return True

    def search(self, q, hints):
        return []

    def test(self):
        raise NotImplementedError


# ==================== Steam (sem conta) ====================
class Steam(Provider):
    id, name, needs_account = "steam", "Steam", False
    help = "Não precisa de conta. Cobre jogos de PC: texto em português, desenvolvedora, data, nota e capa."
    STORE = "https://store.steampowered.com"
    CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._cache = {}

    def configured(self):
        return True

    def applies(self, hints):
        p = hints.get("platforms")
        return not p or "pc" in p

    def _details(self, appid):
        if appid not in self._cache:
            data = self.http.json("GET", f"{self.STORE}/api/appdetails",
                                  params={"appids": appid, "l": "brazilian", "cc": "br"})
            d = (data or {}).get(str(appid)) or {}
            self._cache[appid] = d.get("data") if d.get("success") else None
        return self._cache[appid]

    def _map(self, appid, d, tiny=""):
        if d.get("type") not in (None, "game"):  # ignora DLC, trilha sonora, ferramentas
            return None
        date = fmt_date((d.get("release_date") or {}).get("date", ""))
        score = (d.get("metacritic") or {}).get("score")
        return new_candidate(
            "steam", appid, d.get("name", ""), year=parse_year(date), date=date, platform_keys=["pc"],
            platform_text="PC (Steam)", description=clean_text(d.get("short_description", "")), desc_lang="pt",
            genres=[g.get("description", "") for g in d.get("genres") or []],
            developer=", ".join(d.get("developers") or []), rating=round(score / 10, 1) if score else None,
            thumb=d.get("header_image") or tiny, cover=f"{self.CDN}/{appid}/library_600x900.jpg",
            hero=f"{self.CDN}/{appid}/library_hero.jpg")

    def search(self, q, hints):
        out = []
        if hints.get("steam_id"):  # jogo veio da biblioteca da Steam: o id é um acerto exato, sem ambiguidade
            d = self._details(hints["steam_id"])
            c = self._map(hints["steam_id"], d) if d else None
            if c:
                c["rom_match"] = True
                out.append(c)
        data = self.http.json("GET", f"{self.STORE}/api/storesearch/", params={"term": q, "l": "brazilian", "cc": "br"})
        for it in ((data or {}).get("items") or [])[:5]:
            if str(it.get("id")) == str(hints.get("steam_id")):
                continue
            d = self._details(it.get("id"))
            c = self._map(it["id"], d, it.get("tiny_image", "")) if d else None
            if c:
                out.append(c)
        return out

    def test(self):
        if not (self.http.json("GET", f"{self.STORE}/api/storesearch/", params={"term": "portal", "l": "brazilian", "cc": "br"}) or {}).get("items"):
            raise ScraperError("A Steam respondeu, mas sem resultados.")
        return "Steam respondeu normalmente."


# ==================== IGDB (conta Twitch) ====================
class IGDB(Provider):
    id, name = "igdb", "IGDB"
    help = ("Crie um app em dev.twitch.tv/console/apps (a conta Twitch precisa de verificação em 2 fatores; "
            "Client Type: Confidential; OAuth Redirect: http://localhost) e cole o Client ID e o Client Secret.")
    fields = (("client_id", "Client ID", False, True), ("client_secret", "Client Secret", True, True))
    TOKEN, API, IMG = "https://id.twitch.tv/oauth2/token", "https://api.igdb.com/v4", "https://images.igdb.com/igdb/image/upload"
    FIELDS = ("name,summary,first_release_date,total_rating,rating,genres.name,platforms.id,platforms.name,"
              "involved_companies.developer,involved_companies.company.name,"
              "cover.image_id,artworks.image_id,screenshots.image_id")

    def _token(self, force=False):
        if not force and self.cfg.get("access_token") and self.cfg.get("token_expires_at", 0) > time.time() + 60:
            return self.cfg["access_token"]
        data = self.http.json("POST", self.TOKEN, params={
            "client_id": self.cfg["client_id"], "client_secret": self.cfg["client_secret"],
            "grant_type": "client_credentials"})
        if not data or not data.get("access_token"):
            raise ScraperError("O Twitch não devolveu um token válido.")
        self.cfg["access_token"] = data["access_token"]
        self.cfg["token_expires_at"] = time.time() + data.get("expires_in", 0)
        self.persist()
        return data["access_token"]

    def _post(self, endpoint, body):
        for attempt in (0, 1):
            headers = {"Client-ID": self.cfg["client_id"], "Authorization": f"Bearer {self._token(force=bool(attempt))}"}
            try:
                return self.http.json("POST", f"{self.API}/{endpoint}", headers=headers, data=body) or []
            except ScraperError as e:
                if attempt == 0 and "credenciais" in str(e):  # token expirado: renova e tenta de novo
                    continue
                raise

    def _map(self, g):
        plats = g.get("platforms") or []
        art = g.get("artworks") or g.get("screenshots") or []
        cover = (g.get("cover") or {}).get("image_id")
        ts = g.get("first_release_date")
        date = datetime.fromtimestamp(ts, timezone.utc).strftime("%d/%m/%Y") if ts else ""
        rating = g.get("total_rating") or g.get("rating")
        return new_candidate(
            "igdb", g["id"], g.get("name", ""), year=parse_year(date), date=date,
            platform_keys=[k for k in (key_from_igdb(p.get("id")) for p in plats) if k],
            platform_text=", ".join(p.get("name", "") for p in plats[:4]),
            description=(g.get("summary") or "").strip(), desc_lang="en",
            genres=[x.get("name", "") for x in g.get("genres") or []],
            developer=", ".join(c["company"]["name"] for c in g.get("involved_companies") or []
                                if c.get("developer") and (c.get("company") or {}).get("name")),
            rating=round(rating / 10, 1) if rating else None,
            thumb=f"{self.IMG}/t_cover_big/{cover}.jpg" if cover else "",
            cover=f"{self.IMG}/t_cover_big_2x/{cover}.jpg" if cover else "",
            hero=f"{self.IMG}/t_1080p/{art[0]['image_id']}.jpg" if art and art[0].get("image_id") else "")

    def search(self, q, hints):
        esc = q.replace("\\", "\\\\").replace('"', '\\"')
        ids = [i for i in (igdb_id(k) for k in hints.get("platforms") or []) if i]
        where = f"where platforms = ({','.join(map(str, ids))}); " if ids else ""
        rows = self._post("games", f'search "{esc}"; fields {self.FIELDS}; {where}limit 8;')
        if not rows and ids:  # id de plataforma pode estar errado/fora do catálogo: tenta sem filtro
            rows = self._post("games", f'search "{esc}"; fields {self.FIELDS}; limit 8;')
        return [self._map(g) for g in rows if g.get("name")]

    def test(self):
        self._token(force=True)
        self._post("games", "fields name; limit 1;")
        return "Conectado ao IGDB."


# ==================== SteamGridDB (só arte) ====================
class SteamGridDB(Provider):
    id, name, kind = "steamgriddb", "SteamGridDB", "art"
    help = "Entre em steamgriddb.com com sua conta Steam, abra Preferências → API e gere a chave. Traz as melhores capas, ícones e fundos."
    fields = (("api_key", "Chave da API", True, True),)
    BASE = "https://www.steamgriddb.com/api/v2"

    def _get(self, path, **params):
        d = self.http.json("GET", f"{self.BASE}/{path}", ok404=True, params=params or None,
                           headers={"Authorization": f"Bearer {self.cfg['api_key']}"})
        return (d or {}).get("data") or []

    def _lookup(self, title, steam_id):
        if steam_id:
            return "steam", steam_id
        from .base import similarity
        rows = self._get(f"search/autocomplete/{title}")
        rows = sorted(rows, key=lambda r: -similarity(title, r.get("name", "")))
        return ("game", rows[0]["id"]) if rows and similarity(title, rows[0].get("name", "")) >= 0.6 else (None, None)

    def art(self, title, steam_id=None):
        """-> {'cover': [...], 'icon': [...], 'hero': [...]} (cada item: url, thumb, source)."""
        kind, gid = self._lookup(title, steam_id)
        out = {"cover": [], "icon": [], "hero": []}
        if not kind:
            return out
        safe = {"nsfw": "false", "humor": "false"}
        spec = {"cover": ("grids", {"dimensions": "600x900,660x930,342x482", "types": "static", **safe}),
                "hero": ("heroes", {"types": "static", **safe}), "icon": ("icons", safe)}
        for key, (ep, params) in spec.items():
            rows = sorted(self._get(f"{ep}/{kind}/{gid}", **params), key=lambda r: -(r.get("score") or 0))
            out[key] = [{"url": r["url"], "thumb": r.get("thumb") or r["url"], "source": "SteamGridDB"}
                        for r in rows if r.get("url")][:6]
        return out

    def test(self):
        self._get("search/autocomplete/portal")
        return "Chave do SteamGridDB válida."


# ==================== ScreenScraper (retro / ROMs) ====================
class ScreenScraper(Provider):
    id, name = "screenscraper", "ScreenScraper"
    help = ("O ScreenScraper não dá uma “chave” para usuários: a API pede credenciais de DESENVOLVEDOR (Dev ID e "
            "Dev Password), liberadas pela equipe do site para o programa (peça no fórum do screenscraper.fr, área de "
            "desenvolvedores). Usuário e senha são a sua conta normal do site: opcionais, mas aumentam a cota diária. "
            "Ideal para ROMs de emuladores.")
    fields = (("devid", "Dev ID (do desenvolvedor do programa)", False, True),
              ("devpassword", "Dev Password (do desenvolvedor do programa)", True, True),
              ("username", "Seu usuário do screenscraper.fr (opcional)", False, False),
              ("password", "Sua senha do screenscraper.fr (opcional)", True, False))
    BASE = "https://api.screenscraper.fr/api2"
    REG = ("wor", "us", "eu", "ss", "jp")

    # códigos de erro próprios da API do ScreenScraper
    ERRORS = {401: "a API está fechada para quem não tem conta agora (servidor sobrecarregado). Entre com a sua conta do "
                   "screenscraper.fr em Configurações → Scraper.",
              403: "Dev ID ou Dev Password recusados.",
              423: "a API está fechada no momento. Tente mais tarde.",
              426: "esta versão do NeoSD foi bloqueada pelo ScreenScraper. Atualize o NeoSD.",
              429: "limite de buscas simultâneas da sua conta.",
              430: "a sua cota diária de buscas acabou. Ela renova amanhã.",
              431: "muitas buscas sem resultado hoje (limite diário de “não encontrados”). Renova amanhã."}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        import threading
        from collections import deque
        self._one = threading.Lock()  # nunca mais de uma consulta por vez (1 "thread" na regra do ScreenScraper)
        self._minute = deque()
        self.quota = {}
        self._blocked = (0.0, "")
        from .builtin import screenscraper
        self.builtin = screenscraper()
        if self.builtin:  # credenciais do NeoSD já vêm no .exe: o usuário só entra com a própria conta (opcional)
            self.fields = self.fields[2:]
            self.needs_account = False
            self.help = ("As credenciais de desenvolvedor do NeoSD já vêm incluídas: o ScreenScraper funciona sem "
                         "configurar nada. Entre com a sua conta grátis do screenscraper.fr para ter a sua própria "
                         "cota diária e buscas mais rápidas.")

    def applies(self, hints):
        p = hints.get("platforms")
        return not p or any(k != "pc" for k in p)

    def _params(self, **extra):
        devid, devpw = self.cfg.get("devid", ""), self.cfg.get("devpassword", "")
        if not (devid and devpw) and self.builtin:
            devid, devpw = self.builtin
        p = {"devid": devid, "devpassword": devpw, "softname": "NeoSD", "output": "json"}
        if self.cfg.get("username"):
            p.update(ssid=self.cfg["username"], sspassword=self.cfg.get("password", ""))
        p.update(extra)
        return p

    def _region(self, items, order=REG):
        items = [i for i in items or [] if isinstance(i, dict) and i.get("text")]
        for r in order:
            for i in items:
                if i.get("region") == r:
                    return i["text"]
        return items[0]["text"] if items else ""

    def _lang(self, items, key="langue", order=("pt", "en")):
        items = [i for i in items or [] if isinstance(i, dict) and i.get("text")]
        for lang in order:
            for i in items:
                if i.get(key) == lang:
                    return i["text"], lang
        return (items[0]["text"], items[0].get(key, "")) if items else ("", "")

    def _media(self, medias, types):
        for t in types:
            c = [m for m in medias or [] if m.get("type") == t and m.get("url")]
            for r in self.REG:
                for m in c:
                    if m.get("region") == r:
                        return m["url"]
            if c:
                return c[0]["url"]
        return ""

    def _map(self, j):
        sysd = j.get("systeme") or {}
        syskey = key_from_ss(sysd.get("id") if isinstance(sysd, dict) else j.get("systemeid"))
        desc, lang = self._lang(j.get("synopsis"))
        note = (j.get("note") or {}).get("text")
        date = fmt_date(self._region(j.get("dates")))
        genres = [self._lang(g.get("noms"))[0] for g in j.get("genres") or [] if isinstance(g, dict)]
        medias = j.get("medias")
        try:
            rating = round(float(note) / 2, 1) if note else None  # ScreenScraper usa 0-20
        except ValueError:
            rating = None
        cover = self._media(medias, ("box-2D",))
        return new_candidate(
            "screenscraper", j.get("id", ""), self._region(j.get("noms")), year=parse_year(date), date=date,
            platform_keys=[syskey] if syskey else [], platform_text=sysd.get("text", "") if isinstance(sysd, dict) else "",
            description=desc, desc_lang=lang, genres=[g for g in genres if g],
            developer=(j.get("developpeur") or {}).get("text", ""), rating=rating, thumb=cover, cover=cover,
            hero=self._media(medias, ("fanart", "ss")), icon="")

    # ---------- cota (obrigatório pela regra do ScreenScraper: o programa controla o limite do usuário) ----------
    def _call(self, endpoint, **params):
        import time as _t
        until, why = self._blocked
        if _t.time() < until:
            raise ScraperError(why)
        with self._one:
            q = self.quota
            per_min = int(q.get("maxrequestspermin") or 0)
            if per_min:  # respeita o limite por minuto da conta
                now = _t.monotonic()
                while self._minute and now - self._minute[0] > 60:
                    self._minute.popleft()
                if len(self._minute) >= per_min:
                    _t.sleep(max(0, 60 - (now - self._minute[0])) + 0.5)
            self._minute.append(_t.monotonic())
            r = self.http.request("GET", f"{self.BASE}/{endpoint}", ok404=True, keep=tuple(self.ERRORS),
                                  params=self._params(**params))
            if r is None:
                return None
            if r.status_code in self.ERRORS:
                msg = self.ERRORS[r.status_code]
                if r.status_code in (430, 431, 423, 426):  # não adianta insistir: pausa o ScreenScraper
                    self._blocked = (_t.time() + (3600 if r.status_code in (430, 431) else 900), "ScreenScraper: " + msg)
                raise ScraperError("ScreenScraper: " + msg)
            try:
                d = r.json()
            except ValueError:
                raise ScraperError("ScreenScraper respondeu algo inesperado (pode estar em manutenção).")
            u = ((d or {}).get("response") or {}).get("ssuser")
            if isinstance(u, dict):
                self.quota = {k: u.get(k) for k in ("id", "maxthreads", "maxrequestspermin", "requeststoday",
                                                     "maxrequestsperday", "requestskotoday", "maxrequestskoperday")}
                done, cap = int(u.get("requeststoday") or 0), int(u.get("maxrequestsperday") or 0)
                ko, kocap = int(u.get("requestskotoday") or 0), int(u.get("maxrequestskoperday") or 0)
                if (cap and done >= cap) or (kocap and ko >= kocap):
                    self._blocked = (_t.time() + 3600, "ScreenScraper: " + self.ERRORS[430 if cap and done >= cap else 431])
            return d

    def quota_text(self):
        q = self.quota
        if not q.get("maxrequestsperday"):
            return ""
        return f"{q.get('requeststoday', 0)} de {q['maxrequestsperday']} buscas usadas hoje"

    def search(self, q, hints):
        sid = next((ss_id(k) for k in hints.get("platforms") or [] if ss_id(k)), None)
        out = []
        if hints.get("rom") and sid:  # casa pelo nome do arquivo da ROM: é o acerto mais confiável
            d = self._call("jeuInfos.php", romnom=hints["rom"], systemeid=sid, romtype="rom")
            jeu = ((d or {}).get("response") or {}).get("jeu")
            if isinstance(jeu, dict) and jeu.get("noms"):
                c = self._map(jeu)
                c["rom_match"] = True
                out.append(c)
        for with_sys in ((True, False) if sid else (False,)):
            extra = {"recherche": q, **({"systemeid": sid} if with_sys else {})}
            d = self._call("jeuRecherche.php", **extra)
            jeux = [j for j in (((d or {}).get("response") or {}).get("jeux") or []) if j.get("noms")]
            if jeux:
                out += [self._map(j) for j in jeux[:6]]
                break
        return out

    def test(self):
        self.search("mario", {"platforms": []})
        q = self.quota_text()
        return "Conectado ao ScreenScraper." + (f" {q[0].upper() + q[1:]}." if q else "")
