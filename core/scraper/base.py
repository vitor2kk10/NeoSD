"""Infra comum do scraper: HTTP com limite de taxa/retentativa, normalização de títulos e pontuação."""
import difflib
import html
import re
import threading
import time
import unicodedata
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # sem a lib o app ainda abre; o scraper avisa
    requests = None


class ScraperError(Exception):
    """Mensagem já pronta pra mostrar na interface."""


ALLOWED_IMAGE_HOSTS = ("steamstatic.com", "steampowered.com", "igdb.com", "steamgriddb.com", "screenscraper.fr")


def host_allowed(url):
    p = urlparse(url or "")
    host = (p.hostname or "").lower()
    return p.scheme in ("http", "https") and any(host == h or host.endswith("." + h) for h in ALLOWED_IMAGE_HOSTS)


class Http:
    """requests com: intervalo mínimo por servidor (respeita limites das APIs), retentativa em 429/5xx
    e erros traduzidos pra português. `session`/`sleep` são injetáveis nos testes."""
    INTERVALS = {"api.igdb.com": 0.3, "www.steamgriddb.com": 0.25, "store.steampowered.com": 0.4,
                 "api.screenscraper.fr": 1.3}

    def __init__(self, session=None, sleep=time.sleep):
        if session is None:
            if requests is None:
                raise ScraperError("Falta a biblioteca 'requests' (pip install requests).")
            session = requests.Session()
            session.headers["User-Agent"] = "NeoSD/1.0"
        self.session, self.sleep = session, sleep
        self._last, self._lock = {}, threading.Lock()

    def _throttle(self, host):
        wait = 0
        with self._lock:
            now = time.monotonic()
            wait = self.INTERVALS.get(host, 0) - (now - self._last.get(host, 0))
            self._last[host] = now + max(wait, 0)
        if wait > 0:
            self.sleep(wait)

    def request(self, method, url, ok404=False, retries=2, keep=(), **kw):
        """keep: códigos HTTP devolvidos para quem chamou tratar (em vez de virar erro genérico)."""
        host = urlparse(url).hostname or url
        kw.setdefault("timeout", 15)
        r = None
        for attempt in range(retries + 1):
            self._throttle(host)
            try:
                r = self.session.request(method, url, **kw)
            except Exception as e:
                if attempt < retries:
                    self.sleep(1 + attempt)
                    continue
                raise ScraperError(f"Sem conexão com {host}: {e}")
            if r.status_code in keep:
                return r
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                try:
                    wait = float(r.headers.get("Retry-After", 1 + attempt * 2))
                except ValueError:
                    wait = 2
                self.sleep(min(wait, 10))
                continue
            break
        code = r.status_code
        if code == 404 and ok404:
            return None
        if code in (401, 403):
            raise ScraperError(f"{host}: credenciais inválidas ou sem permissão (HTTP {code}).")
        if code == 429:
            raise ScraperError(f"{host}: limite de requisições atingido. Tente de novo mais tarde.")
        if code >= 400:
            raise ScraperError(f"{host} respondeu HTTP {code}.")
        return r

    def json(self, method, url, **kw):
        r = self.request(method, url, **kw)
        if r is None:
            return None
        try:
            return r.json()
        except ValueError:
            raise ScraperError(f"Resposta inesperada de {urlparse(url).hostname} (não veio JSON).")

    def download(self, url, max_bytes=15_000_000):
        if not host_allowed(url):
            raise ScraperError("Endereço de imagem não permitido.")
        r = self.request("GET", url, timeout=30)
        if len(r.content) > max_bytes:
            raise ScraperError("Imagem grande demais.")
        return r.content


# ---------- texto ----------
_MESES = {"jan": 1, "fev": 2, "feb": 2, "mar": 3, "abr": 4, "apr": 4, "mai": 5, "may": 5, "jun": 6, "jul": 7,
          "ago": 8, "aug": 8, "set": 9, "sep": 9, "out": 10, "oct": 10, "nov": 11, "dez": 12, "dec": 12}
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
_NOISE = {"the", "edition", "definitive", "complete", "ultimate", "deluxe", "remastered", "remaster", "goty",
          "hd", "collection", "of", "year", "game"}


def clean_text(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"[ \t]+", " ", s).strip()


def fmt_date(text):
    """'21 de jan. de 2019' | 'Jan 21, 2019' | '2019-01-21' -> '21/01/2019' (senão devolve como veio)."""
    t = (text or "").strip().lower()
    m = re.match(r"(\d{1,2}) de (\w{3})\w*\.? de (\d{4})", t) or None
    if m and m.group(2) in _MESES:
        return f"{int(m.group(1)):02d}/{_MESES[m.group(2)]:02d}/{m.group(3)}"
    m = re.match(r"(\w{3})\w*\.? (\d{1,2}), (\d{4})", t)
    if m and m.group(1) in _MESES:
        return f"{int(m.group(2)):02d}/{_MESES[m.group(1)]:02d}/{m.group(3)}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return (text or "").strip()


def parse_year(text):
    m = re.search(r"\b(19[6-9]\d|20\d\d)\b", str(text or ""))
    return int(m.group(1)) if m else None


def norm_title(t):
    s = unicodedata.normalize("NFKD", (t or "")).encode("ascii", "ignore").decode().lower().replace("&", " and ")
    toks = [_ROMAN.get(w, w) for w in re.sub(r"[^a-z0-9]+", " ", s).split()]
    core = [w for w in toks if w not in _NOISE]
    return " ".join(core or toks)


def similarity(a, b):
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    return 1.0 if na == nb else min(difflib.SequenceMatcher(None, na, nb).ratio(), 0.95)


def new_candidate(provider, ref, title, **kw):
    c = {"id": f"{provider}:{ref}", "provider": provider, "ref": str(ref), "title": title, "year": None,
         "date": "", "platform_keys": [], "platform_text": "", "description": "", "desc_lang": "",
         "genres": [], "developer": "", "rating": None, "thumb": "", "cover": "", "hero": "", "icon": "",
         "rom_match": False}
    c.update(kw)
    return c


def score_candidate(c, query, hints):
    """Nota 0-1: 70% título, 15% ano, 15% plataforma (sem informação = neutro)."""
    sim = 1.0 if c.get("rom_match") else similarity(query, c["title"])
    y, cy = hints.get("year"), c.get("year")
    ys = 0.5 if not (y and cy) else (1.0 if y == cy else 0.6 if abs(y - cy) <= 1 else 0.0)
    pk, cp = set(hints.get("platforms") or []), set(c.get("platform_keys") or [])
    ps = 0.5 if not (pk and cp) else (1.0 if pk & cp else 0.0)
    c["score"] = round(0.7 * sim + 0.15 * ys + 0.15 * ps, 3)
    c["exact"] = sim >= 0.999
    return c
