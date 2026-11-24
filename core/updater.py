"""Atualização automática pelo GitHub Releases.

Como funciona:
  1. O GitHub Actions gera o instalador e publica um Release no repositório (botão "Run workflow", canal estável ou
     teste). Versão estável: tag v1.6.0. Versão de teste: tag v1.6.0-teste.42 (42 = número da execução), marcada
     como "pre-release".
  2. O NeoSD pergunta ao GitHub (API pública, sem login) quais são os Releases, escolhe o mais novo do canal
     (versões de teste só se a opção estiver ligada) e compara com a versão dele (core/_build_info.py, gerado no build).
  3. Baixa o NeoSD-Setup-*.exe, confere o tamanho e o SHA-256 informados pelo GitHub e, quando o usuário manda (ou
     ao abrir o NeoSD de novo, no modo automático), roda o instalador em modo silencioso: /update=1 faz o instalador
     pular AnyFSE/RetroBat e reabrir o NeoSD no fim.

Os Releases precisam ser PÚBLICOS: um repositório privado exigiria um token do GitHub dentro do programa.
"""
import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("neosd.update")
API = "https://api.github.com/repos/{repo}/releases"
SETUP_RE = re.compile(r"^NeoSD-Setup-.*\.exe$", re.I)
TAG_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:-teste\.(\d+))?$", re.I)
BUSY = ("checking", "downloading", "verifying", "installing")
CHECK_EVERY = 6 * 3600


def build_info():
    """Versão, número do build, canal e repositório (core/_build_info.py é gerado pelo build)."""
    try:
        from . import _build_info as b
        return {"version": b.VERSION, "build": int(b.BUILD), "channel": b.CHANNEL, "repo": b.REPO}
    except Exception:
        root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
        try:
            ver = (root / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            ver = "0"
        return {"version": ver, "build": 0, "channel": "local", "repo": ""}


def parse_tag(tag):
    """'v1.6.0' -> ((1,6,0), 1, 0) ; 'v1.6.0-teste.42' -> ((1,6,0), 0, 42). A estável vem depois dos testes dela."""
    m = TAG_RE.match(str(tag or "").strip())
    if not m:
        return None
    ver = tuple(int(x) for x in m.group(1).split("."))
    return (ver + (0,) * (4 - len(ver)))[:4], (0 if m.group(2) else 1), int(m.group(2) or 0)


def current_key(info):
    ver = tuple(int(x) for x in re.findall(r"\d+", info["version"]))
    ver = (ver + (0,) * (4 - len(ver)))[:4]
    return ver, (1 if info["channel"] == "estavel" else 0), int(info.get("build") or 0)


def label(tag):
    """'v1.6.0' -> '1.6.0' ; 'v1.6.0-teste.42' -> '1.6.0 (teste 42)'."""
    k = parse_tag(tag)
    if not k:
        return str(tag or "")
    v = ".".join(str(x) for x in (k[0][:3] if k[0][3] == 0 else k[0]))
    return v if k[1] else f"{v} (teste {k[2]})"


class Updater:
    def __init__(self, settings, data_dir, info=None, http=None, popen=None, app_dir=None, frozen=None, now=time.time):
        self.settings, self.dir = settings, Path(data_dir)
        self.info = info or build_info()
        self._http, self._popen, self.now = http, popen or subprocess.Popen, now
        self.app_dir = Path(app_dir or (Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()))
        self.frozen = getattr(sys, "frozen", False) if frozen is None else frozen
        self.notify = lambda st: None
        self.quit = lambda: None  # fecha o NeoSD para o instalador trocar os arquivos (ligado no main)
        self._lock = threading.Lock()
        self.st = {"state": "idle", "msg": "", "latest": None, "done": 0, "total": 0, "checked": 0}

    # ---------- opções ----------
    def repo(self):
        return str(self.settings.get("update_repo") or self.info.get("repo") or "")

    def auto(self):
        return bool(self.settings.get("update_auto", True))

    def beta(self):
        return bool(self.settings.get("update_beta", self.info.get("channel") == "teste"))

    def can_install(self):
        """Só a versão instalada se atualiza sozinha (rodando pelo Python ou portátil: só avisa)."""
        return self.frozen and not (self.app_dir / "portable.txt").exists() and os.name == "nt"

    def status(self):
        with self._lock:
            st = dict(self.st)
        ready = self.ready_file()
        st.update(current=label(f"v{self.info['version']}" + (f"-teste.{self.info['build']}" if self.info["channel"] == "teste" else "")),
                  channel=self.info["channel"], build=self.info["build"], repo=self.repo(), auto=self.auto(),
                  beta=self.beta(), can_install=self.can_install(), busy=st["state"] in BUSY,
                  ready=bool(ready), ready_label=label(self.settings.get("update_ready_tag", "")) if ready else "")
        return st

    def _set(self, **kw):
        with self._lock:
            self.st.update(kw)
        self.notify(self.status())

    # ---------- procurar ----------
    def _get(self, url, **kw):
        http = self._http
        if http is None:
            import requests as http
        return http.get(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "NeoSD-updater"}, **kw)

    def check(self):
        """-> dict do Release mais novo do canal, se for mais novo que esta versão; None se já está atualizado."""
        repo = self.repo()
        if not repo:
            self._set(state="error", msg="Esta cópia do NeoSD não sabe de qual repositório do GitHub vem.")
            return None
        self._set(state="checking", msg="Procurando atualizações…")
        try:
            r = self._get(API.format(repo=repo), params={"per_page": 20}, timeout=15)
            if r.status_code == 404:
                self._set(state="error", msg="Repositório não encontrado ou privado: as atualizações precisam de Releases públicos.")
                return None
            r.raise_for_status()
            rels = r.json()
        except Exception as e:
            log.info("procurar atualização: %s", e)
            self._set(state="error", msg="Não consegui falar com o GitHub agora (internet?).")
            return None
        best, best_key = None, None
        for rel in rels if isinstance(rels, list) else []:
            if rel.get("draft") or (rel.get("prerelease") and not self.beta()):
                continue
            key = parse_tag(rel.get("tag_name"))
            asset = next((a for a in rel.get("assets") or [] if SETUP_RE.match(a.get("name", ""))), None)
            if key and asset and (best_key is None or key > best_key):
                best, best_key = (rel, asset), key
        latest = None
        if best and best_key > current_key(self.info):
            rel, asset = best
            latest = {"tag": rel["tag_name"], "label": label(rel["tag_name"]), "test": bool(rel.get("prerelease")),
                      "notes": (rel.get("body") or "")[:4000], "page": rel.get("html_url", ""),
                      "name": asset["name"], "url": asset["browser_download_url"], "size": int(asset.get("size") or 0),
                      "digest": (asset.get("digest") or "").lower()}
        self._set(state="available" if latest else "uptodate", latest=latest, checked=self.now(),
                  msg=f"Nova versão: {latest['label']}" if latest else "Você está na versão mais nova.")
        return latest

    # ---------- baixar ----------
    def ready_file(self):
        p = self.settings.get("update_ready_file")
        if not p or not Path(p).is_file():
            return None
        k = parse_tag(self.settings.get("update_ready_tag"))
        return Path(p) if k and k > current_key(self.info) else None

    def download(self, latest=None):
        latest = latest or self.st.get("latest")
        if not latest:
            return False
        with self._lock:
            if self.st["state"] in ("downloading", "verifying", "installing"):
                return False
            self.st["state"] = "downloading"
        threading.Thread(target=self._download, args=(latest,), daemon=True, name="update-dl").start()
        return True

    def _download(self, latest):
        self.dir.mkdir(parents=True, exist_ok=True)
        final = self.dir / latest["name"]
        part = final.with_name(final.name + ".part")
        try:
            if not (final.is_file() and final.stat().st_size == latest["size"]):
                self._set(state="downloading", done=0, total=latest["size"], msg=f"Baixando o NeoSD {latest['label']}…")
                with self._get(latest["url"], stream=True, timeout=30) as r:
                    r.raise_for_status()
                    done, last = 0, 0.0
                    with open(part, "wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            f.write(chunk)
                            done += len(chunk)
                            if time.monotonic() - last > 0.5:
                                last = time.monotonic()
                                self._set(done=done)
                if latest["size"] and part.stat().st_size != latest["size"]:
                    raise IOError("download incompleto")
                os.replace(part, final)
            self._set(state="verifying", msg="Conferindo o arquivo…")
            if latest["digest"].startswith("sha256:"):
                h = hashlib.sha256()
                with open(final, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != latest["digest"][7:]:
                    final.unlink(missing_ok=True)
                    raise IOError("o arquivo veio corrompido")
            for old in self.dir.glob("NeoSD-Setup-*.exe"):  # guarda só a versão nova
                if old != final:
                    old.unlink(missing_ok=True)
            self.settings.set("update_ready_file", str(final))
            self.settings.set("update_ready_tag", latest["tag"])
            self._set(state="ready", msg=f"NeoSD {latest['label']} pronto para instalar.")
        except Exception as e:
            log.warning("baixar atualização: %s", e)
            part.unlink(missing_ok=True)
            self._set(state="error", msg=f"Não consegui baixar a atualização ({e}).")

    # ---------- instalar ----------
    def install(self, relaunch_args=""):
        """Roda o instalador baixado em modo silencioso e fecha o NeoSD; o instalador reabre o NeoSD no fim."""
        exe = self.ready_file()
        if not exe:
            return False, "Nenhuma atualização baixada."
        if not self.can_install():
            return False, "Esta cópia (portátil ou rodando pelo código) não se atualiza sozinha. Baixe a versão nova no GitHub."
        all_users = "program files" in str(self.app_dir).lower()
        args = [str(exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/update=1",
                "/ALLUSERS" if all_users else "/CURRENTUSER", f"/DIR={self.app_dir}"]
        if relaunch_args:
            args.append(f"/relaunch={relaunch_args}")
        log.info("instalando atualização: %s", subprocess.list2cmdline(args))
        try:
            self._popen(args, close_fds=True, creationflags=0x00000008 | 0x00000200 if os.name == "nt" else 0)
        except OSError as e:
            return False, f"Não consegui abrir o instalador: {e}"
        self.settings.set("update_ready_file", "")
        self._set(state="installing", msg="Instalando a atualização. O NeoSD vai reabrir sozinho.")
        threading.Timer(1.0, self.quit).start()
        return True, ""

    # ---------- automático ----------
    def start(self, wait=25):
        """Procura ao abrir (depois de alguns segundos) e a cada 6 h; no modo automático já baixa."""
        def loop():
            time.sleep(wait)
            while True:
                if self.repo():
                    latest = self.check()
                    if latest and self.auto() and self.can_install():
                        rf = self.ready_file()
                        if not (rf and self.settings.get("update_ready_tag") == latest["tag"]):
                            self.download(latest)
                time.sleep(CHECK_EVERY)
        threading.Thread(target=loop, daemon=True, name="update-check").start()
