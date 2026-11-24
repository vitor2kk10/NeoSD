"""Aviso de conquista POR CIMA do jogo, no estilo dos consoles.

É uma janela nativa do Windows, separada da janela do NeoSD:
  - sempre no topo (WS_EX_TOPMOST) e fora da barra de tarefas e do Alt+Tab (WS_EX_TOOLWINDOW);
  - não rouba o foco do jogo (WS_EX_NOACTIVATE + SW_SHOWNOACTIVATE) e deixa o clique passar (WS_EX_TRANSPARENT);
  - desenhada com transparência por pixel (UpdateLayeredWindow): cantos arredondados, sombra, deslizar e sumir.
O cartão é desenhado com o Pillow (fonte Segoe UI do Windows).

Limite do Windows: um jogo em tela cheia EXCLUSIVA (modo antigo do DirectX 9/10/11) não deixa nenhuma janela
aparecer por cima. Em "tela cheia sem bordas"/"janela sem bordas" e na tela cheia otimizada do Windows 10/11
(a maioria dos jogos DirectX 12/Vulkan) o aviso aparece normalmente.
"""
import ctypes
import io
import logging
import math
import os
import queue
import struct
import threading
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger("neosd.overlay")
W, H, PAD, RADIUS = 400, 92, 18, 20  # tamanho do cartão (em 100% de escala) e margem para a sombra
GOLD = (243, 200, 106, 255)
FONT_DIRS = [Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts", Path("/usr/share/fonts/truetype/dejavu")]
FONTS = {"regular": ("segoeui.ttf", "DejaVuSans.ttf"), "bold": ("segoeuib.ttf", "DejaVuSans-Bold.ttf"),
         "semi": ("seguisb.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf")}


# ---------- desenho (funciona em qualquer sistema; testável) ----------
def _font(kind, size):
    for name in FONTS[kind]:
        for d in FONT_DIRS:
            p = d / name
            if p.is_file():
                try:
                    return ImageFont.truetype(str(p), size)
                except OSError:
                    pass
    return ImageFont.load_default(size)


def _fit(draw, text, font, width):
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _trophy(draw, cx, cy, s, color):
    """Taça desenhada com formas (o emoji colorido não sai bem no Pillow)."""
    draw.pieslice([cx - 5 * s, cy - 9 * s, cx + 5 * s, cy + 3 * s], 0, 180, fill=color)
    draw.rectangle([cx - 5 * s, cy - 9 * s, cx + 5 * s, cy - 3 * s], fill=color)
    draw.arc([cx - 9 * s, cy - 8 * s, cx - 2 * s, cy - 1 * s], 90, 270, fill=color, width=max(1, round(1.6 * s)))
    draw.arc([cx + 2 * s, cy - 8 * s, cx + 9 * s, cy - 1 * s], 270, 90, fill=color, width=max(1, round(1.6 * s)))
    draw.rectangle([cx - 1.2 * s, cy + 2 * s, cx + 1.2 * s, cy + 6 * s], fill=color)
    draw.rounded_rectangle([cx - 4.5 * s, cy + 6 * s, cx + 4.5 * s, cy + 8.5 * s], radius=1 * s, fill=color)


def render_card(name, subtitle="", icon=None, scale=1.0, label="CONQUISTA DESBLOQUEADA"):
    """-> imagem RGBA do aviso (com margem transparente para a sombra)."""
    s = max(1.0, float(scale))
    w, h, pad, r = round(W * s), round(H * s), round(PAD * s), round(RADIUS * s)
    img = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    shadow = Image.new("L", img.size, 0)
    ImageDraw.Draw(shadow).rounded_rectangle([pad, pad + 4 * s, pad + w, pad + h + 4 * s], radius=r, fill=150)
    shadow = shadow.filter(ImageFilter.GaussianBlur(9 * s))
    img.paste((0, 0, 0, 255), (0, 0), shadow)

    card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    grad = Image.linear_gradient("L").rotate(90).resize((w, h))  # brilho sutil da esquerda para a direita
    base = Image.composite(Image.new("RGBA", (w, h), (30, 28, 44, 246)), Image.new("RGBA", (w, h), (16, 16, 24, 246)), grad)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    card.paste(base, (0, 0), mask)
    d = ImageDraw.Draw(card)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, outline=(255, 255, 255, 34), width=max(1, round(s)))

    # ícone
    isz, ix, iy = round(64 * s), round(14 * s), (h - round(64 * s)) // 2
    if icon is not None:
        ic = icon.convert("RGBA").resize((isz, isz), Image.LANCZOS)
        m = Image.new("L", (isz, isz), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, isz - 1, isz - 1], radius=round(12 * s), fill=255)
        card.paste(ic, (ix, iy), ImageChops.multiply(m, ic.getchannel("A")))
    else:
        d.rounded_rectangle([ix, iy, ix + isz, iy + isz], radius=round(12 * s), fill=(255, 255, 255, 18))
        _trophy(d, ix + isz / 2, iy + isz / 2, 2.1 * s, GOLD)

    # textos
    tx, tw = ix + isz + round(14 * s), w - (ix + isz + round(14 * s)) - round(18 * s)
    f_lab, f_name, f_sub = _font("bold", round(11 * s)), _font("semi", round(16.5 * s)), _font("regular", round(13 * s))
    _trophy(d, tx + 6 * s, round(25 * s), 0.72 * s, GOLD)
    d.text((tx + 15 * s, round(17.5 * s)), label, font=f_lab, fill=GOLD)
    d.text((tx, round(34 * s)), _fit(d, name, f_name, tw), font=f_name, fill=(245, 245, 250, 255))
    if subtitle:
        d.text((tx, round(58 * s)), _fit(d, subtitle, f_sub, tw), font=f_sub, fill=(170, 170, 190, 255))
    img.alpha_composite(card, (pad, pad))
    return img


def chime(rate=44100):
    """Som curto de 'ding' (duas notas suaves), gerado na hora em WAV."""
    notes, out = [(1318.5, 0.0, 0.55), (1975.5, 0.09, 0.6)], []
    total = int(rate * 0.75)
    for i in range(total):
        t, v = i / rate, 0.0
        for f, start, dur in notes:
            if start <= t < start + dur:
                k = t - start
                env = min(1.0, k / 0.006) * math.exp(-k * 7.5)
                v += math.sin(2 * math.pi * f * k) * env * 0.16 + math.sin(4 * math.pi * f * k) * env * 0.03
        out.append(int(max(-1, min(1, v)) * 32767))
    data = struct.pack(f"<{len(out)}h", *out)
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


# ---------- janela do Windows ----------
class OverlayService:
    def __init__(self, cache_dir, settings=None, http=None):
        self.cache_dir, self.settings, self._http = Path(cache_dir), settings, http
        self._q = queue.Queue()
        self._thread = None
        self._wav = None
        self.sfx = None  # SfxService (ligado no main)

    def enabled(self):
        return os.name == "nt" and (self.settings is None or self.settings.get("ach_overlay", True))

    def show(self, name, subtitle="", icon_url="", key="", label="CONQUISTA DESBLOQUEADA"):
        if not self.enabled():
            return False
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True, name="overlay")
            self._thread.start()
        self._q.put((name, subtitle, icon_url, key, label))
        return True

    def _icon(self, url, key):
        if not url:
            return None
        f = self.cache_dir / (re_safe(key or url.rsplit("/", 1)[-1]))
        try:
            if not f.is_file():
                http = self._http
                if http is None:
                    import requests as http
                r = http.get(url, timeout=5)
                r.raise_for_status()
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                f.write_bytes(r.content)
            return Image.open(io.BytesIO(f.read_bytes()))
        except Exception as e:
            log.info("ícone da conquista indisponível: %s", e)
            return None

    def _sound(self):
        if self.settings is not None and not self.settings.get("ach_sound", True):
            return
        if self.sfx is not None and self.sfx.play("trophy"):  # som de conquista da pasta SFX, se houver
            return
        try:
            import winsound
            self._wav = self._wav or chime()
            threading.Thread(target=winsound.PlaySound, args=(self._wav, winsound.SND_MEMORY), daemon=True).start()
        except Exception:
            pass

    def _run(self):
        try:
            win = _LayeredWindow()
        except Exception:
            log.exception("não consegui criar a janela de aviso")
            return
        while True:
            name, sub, url, key, label = self._q.get()
            try:
                icon = self._icon(url, key)
                x, y, mw, scale = win.anchor()
                img = render_card(name, sub, icon, scale, label)
                pad = round(PAD * scale)
                left, top = x + mw - img.width + pad - round(10 * scale), y + round(14 * scale) - pad
                self._sound()
                win.animate(img, left, top, scale)
            except Exception:
                log.exception("aviso de conquista")


def re_safe(s):
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in s)[:120]


class _LayeredWindow:
    """Janela 'em camadas' do Win32 criada e usada só pela thread do aviso."""

    def __init__(self):
        from ctypes import wintypes as wt
        self.wt = wt
        u, g, k = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32
        self.u, self.g = u, g
        try:  # coordenadas em pixels reais, para o desenho não ficar borrado em telas com escala
            u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        vp = ctypes.c_void_p
        for fn, res, args in (
                (u.CreateWindowExW, vp, [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, vp, vp, vp, vp]),
                (u.DefWindowProcW, ctypes.c_ssize_t, [vp, wt.UINT, wt.WPARAM, wt.LPARAM]),
                (u.GetDC, vp, [vp]), (u.ReleaseDC, ctypes.c_int, [vp, vp]),
                (u.UpdateLayeredWindow, wt.BOOL, [vp, vp, vp, vp, vp, vp, wt.DWORD, vp, wt.DWORD]),
                (u.ShowWindow, wt.BOOL, [vp, ctypes.c_int]),
                (u.SetWindowPos, wt.BOOL, [vp, vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]),
                (u.GetForegroundWindow, vp, []), (u.MonitorFromWindow, vp, [vp, wt.DWORD]),
                (u.GetMonitorInfoW, wt.BOOL, [vp, vp]),
                (u.PeekMessageW, wt.BOOL, [vp, vp, wt.UINT, wt.UINT, wt.UINT]),
                (u.TranslateMessage, wt.BOOL, [vp]), (u.DispatchMessageW, ctypes.c_ssize_t, [vp]),
                (g.CreateCompatibleDC, vp, [vp]), (g.DeleteDC, wt.BOOL, [vp]),
                (g.CreateDIBSection, vp, [vp, vp, wt.UINT, ctypes.POINTER(vp), vp, wt.DWORD]),
                (g.SelectObject, vp, [vp, vp]), (g.DeleteObject, wt.BOOL, [vp]),
                (k.GetModuleHandleW, vp, [wt.LPCWSTR])):
            fn.restype, fn.argtypes = res, args
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, vp, wt.UINT, wt.WPARAM, wt.LPARAM)
        self._proc = WNDPROC(lambda h, m, w, l: u.DefWindowProcW(h, m, w, l))  # referência mantida viva

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", vp), ("hIcon", vp), ("hCursor", vp),
                        ("hbrBackground", vp), ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]
        hinst = k.GetModuleHandleW(None)
        wc = WNDCLASSW(0, self._proc, 0, 0, hinst, None, None, None, None, "NeoSDAchievementToast")
        u.RegisterClassW(ctypes.byref(wc))
        ex = 0x00080000 | 0x00000008 | 0x00000080 | 0x08000000 | 0x00000020  # LAYERED|TOPMOST|TOOLWINDOW|NOACTIVATE|TRANSPARENT
        self.hwnd = u.CreateWindowExW(ex, "NeoSDAchievementToast", "NeoSD", 0x80000000, 0, 0, 1, 1,
                                      None, None, hinst, None)
        if not self.hwnd:
            raise OSError("CreateWindowExW falhou")

    def anchor(self):
        """Canto superior direito do monitor onde está o jogo (janela em primeiro plano). -> x, y, largura, escala."""
        wt = self.wt

        class MI(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]
        mon = self.u.MonitorFromWindow(self.u.GetForegroundWindow(), 1)
        mi = MI()
        mi.cbSize = ctypes.sizeof(MI)
        self.u.GetMonitorInfoW(mon, ctypes.byref(mi))
        rc = mi.rcMonitor
        scale = 1.0
        try:
            dx, dy = wt.UINT(), wt.UINT()
            if ctypes.windll.shcore.GetDpiForMonitor(ctypes.c_void_p(mon), 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
                scale = dx.value / 96
        except Exception:
            pass
        return rc.left, rc.top, rc.right - rc.left, scale

    def _pump(self, seconds):
        msg = self.wt.MSG()
        end = time.monotonic() + seconds
        while True:
            while self.u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                self.u.TranslateMessage(ctypes.byref(msg))
                self.u.DispatchMessageW(ctypes.byref(msg))
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.016, left))

    def animate(self, img, x, y, scale, hold=4.6):
        wt, u, g = self.wt, self.u, self.g
        w, h = img.size
        r, gr, b, a = img.split()  # o Windows espera BGRA com cor pré-multiplicada pelo alfa
        pre = Image.merge("RGBA", (ImageChops.multiply(r, a), ImageChops.multiply(gr, a), ImageChops.multiply(b, a), a))
        raw = pre.tobytes("raw", "BGRA")

        class BIH(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                        ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

        class BLEND(ctypes.Structure):
            _fields_ = [("op", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("alpha", ctypes.c_ubyte), ("fmt", ctypes.c_ubyte)]
        bih = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        screen = u.GetDC(None)
        mem = g.CreateCompatibleDC(screen)
        bits = ctypes.c_void_p()
        bmp = g.CreateDIBSection(screen, ctypes.byref(bih), 0, ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, raw, len(raw))
        old = g.SelectObject(mem, bmp)
        size, src = wt.SIZE(w, h), wt.POINT(0, 0)

        def frame(alpha, dx):
            pt = wt.POINT(int(x + dx), int(y))
            u.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(pt), ctypes.byref(size), mem, ctypes.byref(src), 0,
                                  ctypes.byref(BLEND(0, 0, max(0, min(255, int(alpha))), 1)), 2)

        try:
            frame(0, 40 * scale)
            u.SetWindowPos(self.hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)  # TOPMOST, sem ativar
            u.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
            steps = 16
            for i in range(1, steps + 1):  # desliza da direita e aparece
                t = i / steps
                ease = 1 - (1 - t) ** 3
                frame(255 * ease, 40 * scale * (1 - ease))
                self._pump(0.016)
            self._pump(hold)
            for i in range(1, 13):  # some
                frame(255 * (1 - i / 12), 0)
                self._pump(0.016)
        finally:
            u.ShowWindow(self.hwnd, 0)
            g.SelectObject(mem, old)
            g.DeleteObject(bmp)
            g.DeleteDC(mem)
            u.ReleaseDC(None, screen)
