"""Baixar e instalar o RetroBat oficial pelo NeoSD.

O NeoSD não leva o RetroBat dentro do instalador: ele é grande, muda de versão com frequência e tem licença
própria (sem uso comercial). Aqui o NeoSD só faz o que o usuário faria à mão:
  1. pergunta ao GitHub oficial (RetroBat-Official/retrobat) qual é a versão mais nova;
  2. baixa o instalador oficial (…-setup.exe), retomando de onde parou se a internet cair;
  3. confere o tamanho e, quando o GitHub informa, o SHA-256 do arquivo;
  4. abre o instalador oficial NA FRENTE do NeoSD: o usuário lê a licença, escolhe a pasta e instala;
  5. quando o instalador fecha, encontra o RetroBat sozinho e traz os jogos para a biblioteca.
"""
import hashlib
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger("neosd.rbsetup")
API_LATEST = "https://api.github.com/repos/RetroBat-Official/retrobat/releases/latest"
RELEASES_PAGE = "https://github.com/RetroBat-Official/retrobat/releases/latest"
UA = "NeoSD (instalador do RetroBat)"
SETUP_RE = re.compile(r"setup.*\.exe$", re.I)
BUSY = ("checking", "downloading", "verifying", "installing")


def pick_asset(release):
    """O instalador do Windows 64 bits entre os arquivos da versão (ex.: RetroBat-v8.4.0-stable-win64-setup.exe)."""
    exes = [a for a in release.get("assets") or [] if SETUP_RE.search(a.get("name", ""))]
    exes.sort(key=lambda a: ("win64" not in a["name"].lower(), "stable" not in a["name"].lower()))
    return exes[0] if exes else None


def mb(n):
    return f"{n / 1048576:,.0f} MB".replace(",", ".")


class RetroBatSetup:
    def __init__(self, retrobat, folder, notify=None, http=None, runner=None):
        self.rb, self.folder = retrobat, Path(folder)
        self.notify = notify or (lambda st: None)
        self._http, self._runner = http, runner
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self.on_installed = lambda root: None
        self.on_install_start = self.on_install_end = lambda: None  # ex.: sair da frente no modo console
        self.st = {"state": "idle", "done": 0, "total": 0, "version": "", "msg": "", "page": RELEASES_PAGE}

    # ---------- estado ----------
    def status(self):
        with self._lock:
            st = dict(self.st)
        root = self.rb.root()
        if st["state"] == "notfound" and root:  # instalou numa pasta que só agora apareceu
            self._found(root)
            st = dict(self.st)
        st["found"] = bool(root)
        st["busy"] = st["state"] in BUSY
        return st

    def _set(self, **kw):
        with self._lock:
            self.st.update(kw)
            st = dict(self.st)
        st["busy"] = st["state"] in BUSY
        self.notify(st)

    # ---------- ações ----------
    def start(self):
        with self._lock:
            if self.st["state"] in BUSY:
                return False, "O RetroBat já está sendo baixado."
        if self.rb.root():
            return False, "O RetroBat já está instalado."
        self._cancel.clear()
        self._set(state="checking", done=0, total=0, msg="Procurando a versão mais nova…")
        threading.Thread(target=self._run, daemon=True, name="rbsetup").start()
        return True, ""

    def cancel(self):
        if self.st["state"] == "downloading" or self.st["state"] == "checking":
            self._cancel.set()
            return True
        return False

    # ---------- trabalho ----------
    def _session(self):
        if self._http:
            return self._http
        import requests
        s = requests.Session()
        s.headers["User-Agent"] = UA
        return s

    def _run(self):
        try:
            http = self._session()
            r = http.get(API_LATEST, timeout=20, headers={"Accept": "application/vnd.github+json"})
            r.raise_for_status()
            rel = r.json()
            asset = pick_asset(rel)
            ver = rel.get("tag_name") or rel.get("name") or ""
            if not asset:
                return self._fail("Não achei o instalador na página oficial. Baixe por lá: " + RELEASES_PAGE, ver)
            size, name = int(asset.get("size") or 0), Path(asset["name"]).name
            self.folder.mkdir(parents=True, exist_ok=True)
            final = self.folder / name
            if not (final.is_file() and size and final.stat().st_size == size):
                free = shutil.disk_usage(self.folder).free
                if size and free < size + 300 * 1048576:
                    return self._fail(f"Falta espaço: o instalador tem {mb(size)} e o disco tem {mb(free)} livres.", ver)
                if not self._download(http, asset["browser_download_url"], final, size, ver):
                    return
            self._set(state="verifying", done=size, total=size, version=ver, msg="Conferindo o arquivo…")
            digest = (asset.get("digest") or "").lower()
            if digest.startswith("sha256:") and self._sha256(final) != digest[7:]:
                final.unlink(missing_ok=True)
                return self._fail("O arquivo baixado veio corrompido. Tente de novo.", ver)
            self._install(final, ver)
        except Exception as e:
            log.exception("baixar o RetroBat")
            self._fail(f"Não consegui baixar o RetroBat ({type(e).__name__}). Verifique a internet e tente de novo.")

    def _download(self, http, url, final, size, ver):
        part = final.with_name(final.name + ".part")
        have = part.stat().st_size if part.is_file() else 0
        if size and have > size:
            part.unlink()
            have = 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        with http.get(url, stream=True, timeout=30, headers=headers, allow_redirects=True) as r:
            if r.status_code == 416:  # já estava completo
                r.close()
            else:
                r.raise_for_status()
                if have and r.status_code != 206:  # o servidor ignorou o Range: recomeça
                    have = 0
                self._set(state="downloading", done=have, total=size, version=ver,
                          msg=f"Baixando o RetroBat {ver}…")
                last = 0.0
                with open(part, "ab" if have else "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        if self._cancel.is_set():
                            self._set(state="cancelled", msg="Download pausado. Continue quando quiser: ele retoma de onde parou.")
                            return False
                        f.write(chunk)
                        have += len(chunk)
                        if time.monotonic() - last > 0.5:
                            last = time.monotonic()
                            self._set(done=have)
        got = part.stat().st_size
        if size and got != size:
            return self._fail(f"O download parou no meio ({mb(got)} de {mb(size)}). Tente de novo: ele continua de onde parou.", ver)
        os.replace(part, final)
        return True

    @staticmethod
    def _sha256(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def _install(self, exe, ver):
        self._set(state="installing", msg="Siga o instalador do RetroBat. Dica: instale em C:\\RetroBat "
                                          "(evite Arquivos de Programas e pastas do OneDrive).")
        run = self._runner
        if run is None:
            from .elevate import shell_run as run
        self._hook(self.on_install_start)
        try:
            status, code = run(exe, "", "open", 1, 4 * 3600, exe.parent)
        finally:
            self._hook(self.on_install_end)
        log.info("instalador do RetroBat terminou: %s (código %s)", status, code)
        if status == "cancelled":
            return self._set(state="idle", msg="Instalação cancelada no pedido de administrador.")
        if status.startswith("error:"):
            return self._fail("Não consegui abrir o instalador do RetroBat: " + status[6:], ver)
        self.rb.forget()
        for _ in range(10):  # alguns instaladores terminam um pouco depois de fechar a janela
            root = self.rb.root()
            if root:
                exe.unlink(missing_ok=True)  # o instalador ocupa bastante espaço e não serve para atualizar
                return self._found(root)
            time.sleep(1)
        self._set(state="notfound", msg="Não encontrei o RetroBat. Se você instalou em outra pasta, "
                                        "indique em Configurações → Emulação → Escolher pasta.")

    @staticmethod
    def _hook(fn):
        try:
            fn()
        except Exception:
            log.exception("RetroBat: janela")

    def _found(self, root):
        self._set(state="done", msg=f"RetroBat instalado em {root}.")
        try:
            self.on_installed(root)
        except Exception:
            log.exception("depois de instalar o RetroBat")

    def _fail(self, msg, ver=None):
        log.warning("RetroBat: %s", msg)
        kw = {"state": "error", "msg": msg}
        if ver is not None:
            kw["version"] = ver
        self._set(**kw)
        return False
