"""Ícones dos apps: extrai o ícone do executável (Windows) e guarda um PNG em cache em library/appicons/.
Ordem: PrivateExtractIcons (até 256 px, via ctypes) -> PowerShell/System.Drawing (32 px) -> sem ícone (a interface
mostra as iniciais). Qualquer falha cai pro próximo passo, nunca derruba o app."""
import base64
import ctypes
import hashlib
import io
import os
import subprocess
import sys
import time

from PIL import Image

from .store import LIBRARY

CACHE = LIBRARY / "appicons"
RETRY_AFTER = 7 * 86400  # tentativa que falhou só é repetida depois de uma semana


def compose_icon(color_bgra, w, h, mask_bgra=None):
    """Pixels BGRA do HBITMAP (+ máscara AND dos ícones antigos, sem canal alfa) -> imagem RGBA."""
    img = Image.frombuffer("RGBA", (w, h), bytes(color_bgra), "raw", "BGRA", 0, 1).copy()
    if img.getchannel("A").getextrema()[1] == 0:  # ícone sem alfa: a máscara decide (preto = opaco)
        if mask_bgra:
            m = Image.frombuffer("RGBA", (w, h), bytes(mask_bgra), "raw", "BGRA", 0, 1).convert("L")
            alpha = m.point(lambda v: 0 if v > 127 else 255)
        else:
            alpha = Image.new("L", (w, h), 255)
        img.putalpha(alpha)
    return img


def _extract_win(path, size):
    from ctypes import wintypes as wt

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                    ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", wt.LONG), ("bmWidth", wt.LONG), ("bmHeight", wt.LONG), ("bmWidthBytes", wt.LONG),
                    ("bmPlanes", wt.WORD), ("bmBitsPixel", wt.WORD), ("bmBits", wt.LPVOID)]

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    u, g = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")  # DLLs próprias: não mexe nos argtypes globais
    u.PrivateExtractIconsW.argtypes = [wt.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(wt.HICON),
                                       ctypes.POINTER(wt.UINT), wt.UINT, wt.UINT]
    u.PrivateExtractIconsW.restype = wt.UINT
    u.GetIconInfo.argtypes, u.GetIconInfo.restype = [wt.HICON, ctypes.POINTER(ICONINFO)], wt.BOOL
    u.GetDC.argtypes, u.GetDC.restype = [wt.HWND], wt.HDC
    u.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    u.DestroyIcon.argtypes = [wt.HICON]
    g.GetObjectW.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p]
    g.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
    g.GetDIBits.restype = ctypes.c_int
    g.DeleteObject.argtypes = [wt.HGDIOBJ]

    hicon, iid = wt.HICON(), wt.UINT()
    if u.PrivateExtractIconsW(path, 0, size, size, ctypes.byref(hicon), ctypes.byref(iid), 1, 0) < 1 or not hicon.value:
        return None
    info, hdc = ICONINFO(), None
    try:
        if not u.GetIconInfo(hicon, ctypes.byref(info)) or not info.hbmColor:
            return None
        bm = BITMAP()
        g.GetObjectW(info.hbmColor, ctypes.sizeof(BITMAP), ctypes.byref(bm))
        w, h = bm.bmWidth, bm.bmHeight
        bih = BIH(biSize=ctypes.sizeof(BIH), biWidth=w, biHeight=-h, biPlanes=1, biBitCount=32, biCompression=0)
        hdc = u.GetDC(None)
        color, mask = ctypes.create_string_buffer(w * h * 4), ctypes.create_string_buffer(w * h * 4)
        g.GetDIBits(hdc, info.hbmColor, 0, h, color, ctypes.byref(bih), 0)
        g.GetDIBits(hdc, info.hbmMask, 0, h, mask, ctypes.byref(bih), 0)
        return compose_icon(color.raw, w, h, mask.raw)
    finally:
        if hdc:
            u.ReleaseDC(None, hdc)
        for hb in (info.hbmColor, info.hbmMask):
            if hb:
                g.DeleteObject(hb)
        u.DestroyIcon(hicon)


def _extract_powershell(path, cache=CACHE):
    out = cache / f"_tmp_{os.getpid()}.png"
    q = lambda s: str(s).replace("'", "''")
    ps = ("Add-Type -AssemblyName System.Drawing;"
          f"$i=[System.Drawing.Icon]::ExtractAssociatedIcon('{q(path)}');"
          f"$i.ToBitmap().Save('{q(out)}',[System.Drawing.Imaging.ImageFormat]::Png)")
    try:
        cache.mkdir(parents=True, exist_ok=True)
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], capture_output=True,
                       timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with Image.open(out) as im:
            return im.convert("RGBA").copy()
    except Exception:
        return None
    finally:
        try:
            out.unlink()
        except OSError:
            pass


def extract_icon(path):
    """Imagem RGBA do ícone do executável, ou None."""
    if sys.platform != "win32" or not os.path.isfile(path):
        return None
    for size in (256, 64, 32):
        try:
            img = _extract_win(path, size)
        except Exception:
            img = None
        if img is not None and img.getchannel("A").getbbox():
            return img
    return _extract_powershell(path)


def icon_for(path, cache=CACHE, extract=extract_icon):
    """data URI (PNG até 128 px) do ícone de `path`, com cache em disco; None se não houver ícone."""
    if not path or not os.path.isfile(path):
        return None
    try:
        key = hashlib.sha1(f"{os.path.normcase(path)}|{os.path.getmtime(path)}".encode()).hexdigest()[:16]
        png, none = cache / f"{key}.png", cache / f"{key}.none"
        if png.exists():
            return "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode()
        if none.exists() and time.time() - none.stat().st_mtime < RETRY_AFTER:
            return None
        cache.mkdir(parents=True, exist_ok=True)
        img = extract(path)
        if img is None or not img.getchannel("A").getbbox():
            none.touch()
            return None
        img = img.convert("RGBA")
        img.thumbnail((128, 128), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        png.write_bytes(buf.getvalue())
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None
