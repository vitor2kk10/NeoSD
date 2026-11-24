"""Fundo do modo console: banner do jogo inteiro (sem recorte) e papéis de parede.

Banner: a arte 'hero' (Steam: 3840x1240, ~21:9) é mostrada na largura da tela, presa no topo, sem cortar
nada e sem ampliar além do tamanho original; a parte de baixo some num degradê atrás da fileira de jogos
(isso é CSS, no HTML). Jogo sem banner = fundo padrão (cores do jogo), nunca a capa esticada.

As imagens prontas ficam em library/bgcache e chegam ao navegador por link do MediaServer (127.0.0.1).
Se o servidor não subir, volta para data URI.

Modos (app_settings.json -> console_bg):
  game       banner do jogo; sem banner, fundo padrão
  wallpaper  papel de parede fixo (imagem da pasta library/wallpapers ou um tema de cor embutido)
  colors     só as cores do jogo
"""
import base64
import hashlib
import io
import logging
import os
import shutil
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger("neosd.backdrop")

MODES = ("game", "wallpaper", "colors")
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
MAX_W, MAX_H = 2560, 1440
NONE = {"url": "", "aspect": 0}


def _clamp_size(w, h):
    try:
        w, h = int(w), int(h)
    except (TypeError, ValueError):
        w, h = 1920, 1080
    w, h = max(640, w), max(360, h)
    k = min(1.0, MAX_W / w, MAX_H / h)
    return round(w * k), round(h * k)


def render_banner(im, w):
    """Mantém a proporção; só reduz (nunca amplia) para a largura da tela."""
    im = im.convert("RGB")
    if im.width > w:
        im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
    return im


def render_fill(im, w, h):
    """Papel de parede: preenche a tela (como background-size: cover) já no tamanho final."""
    return ImageOps.fit(im.convert("RGB"), (w, h), Image.LANCZOS, centering=(0.5, 0.5))


class BackdropService:
    def __init__(self, settings, base, resolve_art, server=None, wall_dir=None, cache_dir=None):
        """resolve_art(game_id, kind) -> Path | None. server: MediaServer (opcional)."""
        self.settings, self.resolve_art, self.server = settings, resolve_art, server
        base = Path(base)
        self.wall_dir = Path(wall_dir or base / "library" / "wallpapers")
        self.cache_dir = Path(cache_dir or base / "library" / "bgcache")
        if server:
            server.add_root("bg", self.cache_dir)

    # ---------- cache em disco ----------
    def _render(self, src, tag, build, quality=90):
        """-> (link ou data URI, largura, altura). O nome do arquivo muda quando a origem muda."""
        st = os.stat(src)
        key = hashlib.sha1(f"{src}|{st.st_mtime_ns}|{st.st_size}|{tag}|v4".encode()).hexdigest()
        path = self.cache_dir / f"{key}.jpg"
        data = None
        if path.is_file():
            with Image.open(path) as im:
                size = im.size
        else:
            with Image.open(src) as im:
                out = build(ImageOps.exif_transpose(im))
            size = out.size
            buf = io.BytesIO()
            out.save(buf, "JPEG", quality=quality)  # sem progressive/optimize: falham em Pillows antigos
            data = buf.getvalue()
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                self._trim()
            except OSError:
                log.exception("não consegui gravar o cache de fundo em %s", self.cache_dir)
        url = self.server.url("bg", path.name) if self.server and path.is_file() else ""
        if not url:
            data = data if data is not None else path.read_bytes()
            url = f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"
        return url, size[0], size[1]

    def _trim(self, keep=200):
        files = sorted(self.cache_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)  # PNG de miniaturas ficam
        for p in files[keep:]:
            try:
                p.unlink()
            except OSError:
                pass

    # ---------- miniaturas (logos de emulador etc.) ----------
    def thumb(self, src, size=256):
        """PNG reduzido (mantém transparência), em cache; devolve link do servidor local ou data URI."""
        src = Path(src)
        if not src.is_file():
            return ""
        st = os.stat(src)
        key = hashlib.sha1(f"{src}|{st.st_mtime_ns}|{st.st_size}|t{size}".encode()).hexdigest()
        path = self.cache_dir / f"{key}.png"
        if not path.is_file():
            with Image.open(src) as im:
                im = im.convert("RGBA")
                im.thumbnail((size, size), Image.LANCZOS)
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                im.save(path, "PNG", optimize=True)
        url = self.server.url("bg", path.name) if self.server else ""
        return url or f"data:image/png;base64,{base64.b64encode(path.read_bytes()).decode()}"

    # ---------- banner de um jogo ----------
    def game(self, game_id, w, h):
        """-> {'url', 'aspect'} (largura/altura do banner) ou url '' se o jogo não tiver banner."""
        w, h = _clamp_size(w, h)
        try:
            hero = self.resolve_art(game_id, "hero")
            if not hero or not Path(hero).is_file():
                return dict(NONE)
            url, iw, ih = self._render(Path(hero), f"banner{w}", lambda im: render_banner(im, w))
            return {"url": url, "aspect": round(iw / ih, 4)}
        except Exception:
            log.exception("falha ao montar o banner de %s", game_id)
            return dict(NONE)

    # ---------- papéis de parede ----------
    def walls(self):
        if not self.wall_dir.is_dir():
            return []
        return sorted((p for p in self.wall_dir.iterdir() if p.suffix.lower() in IMG_EXT and p.is_file()),
                      key=lambda p: p.name.lower())

    def _thumb(self, p):
        try:
            with Image.open(p) as im:
                out = render_fill(ImageOps.exif_transpose(im), 320, 180)
            buf = io.BytesIO()
            out.save(buf, "JPEG", quality=82)
            return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"
        except Exception:
            log.exception("miniatura do papel de parede %s", p)
            return ""

    def wallpaper(self, wid, w, h):
        w, h = _clamp_size(w, h)
        if not wid or wid.startswith("preset:"):
            return dict(NONE)
        p = self.wall_dir / os.path.basename(wid)
        if not p.is_file():
            return dict(NONE)
        try:
            url, iw, ih = self._render(p, f"fill{w}x{h}", lambda im: render_fill(im, w, h))
            return {"url": url, "aspect": round(iw / ih, 4)}
        except Exception:
            log.exception("falha ao abrir o papel de parede %s", p)
            return dict(NONE)

    def add(self, src):
        """Copia uma imagem escolhida pelo usuário para a pasta de papéis de parede. -> (ok, id|msg)."""
        src = Path(src)
        if src.suffix.lower() not in IMG_EXT or not src.is_file():
            return False, "Escolha uma imagem (JPG, PNG, WEBP ou BMP)."
        try:
            with Image.open(src) as im:
                im.verify()
        except Exception:
            return False, "Não consegui abrir essa imagem."
        self.wall_dir.mkdir(parents=True, exist_ok=True)
        dst, n = self.wall_dir / src.name, 2
        while dst.exists() and not dst.samefile(src):
            dst = self.wall_dir / f"{src.stem} ({n}){src.suffix}"
            n += 1
        if not dst.exists():
            shutil.copy2(src, dst)
        return True, dst.name

    def remove(self, wid):
        p = self.wall_dir / os.path.basename(wid or "")
        if p.is_file():
            p.unlink()
            if self.settings.get("console_wall") == wid:
                self.settings.set("console_wall", "preset:aurora")
            return True
        return False

    # ---------- preferências ----------
    def config(self):
        mode = self.settings.get("console_bg", "game")
        return {"mode": mode if mode in MODES else "game",
                "wall": self.settings.get("console_wall", "preset:aurora"),
                "walls": [{"id": p.name, "name": p.stem, "thumb": self._thumb(p)} for p in self.walls()],
                "folder": str(self.wall_dir)}

    def set(self, key, value):
        if key == "mode" and value in MODES:
            self.settings.set("console_bg", value)
            return True
        if key == "wall" and isinstance(value, str):
            self.settings.set("console_wall", value)
            return True
        return False
