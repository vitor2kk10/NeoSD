"""Capas/ícones -> data URI (pro HTML) + cor dominante (pro fundo ambiente)."""
import base64
import hashlib
import io
import math
import os

from PIL import Image, ImageOps

_cache = {}


def dominant_hue(im):
    """Matiz médio (0-360) ponderado por saturação/brilho; None se a arte for cinza."""
    data = im.convert("RGB").resize((24, 24)).convert("HSV").tobytes()
    x = y = 0.0
    for i in range(0, len(data), 3):
        h, s, v = data[i], data[i + 1], data[i + 2]
        w = (s / 255) * (v / 255) ** 0.5
        a = h / 255 * 2 * math.pi
        x += w * math.cos(a)
        y += w * math.sin(a)
    if math.hypot(x, y) < 1e-3:
        return None
    return round(math.degrees(math.atan2(y, x)) % 360)


def hash_hue(text):
    return int(hashlib.md5(text.encode("utf-8")).hexdigest()[:4], 16) % 360


def load_art(path, kind="cover"):
    """-> (data_uri | None, hue | None). Cacheado por arquivo + data de modificação."""
    if not path or not os.path.isfile(path):
        return None, None
    key = (str(path), os.path.getmtime(path), kind)
    if key in _cache:
        return _cache[key]
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            hue = dominant_hue(im)
            buf = io.BytesIO()
            if kind == "hero":
                im = im.convert("RGB")
                im.thumbnail((2560, 1440), Image.LANCZOS)  # Steam: 3840x1240 -> 2560x827 (antes virava 1280x413)
                im.save(buf, "JPEG", quality=86)
                mime = "image/jpeg"
            elif kind == "icon":
                im = im.convert("RGBA")
                im.thumbnail((96, 96))
                im.save(buf, "PNG")
                mime = "image/png"
            else:
                im = im.convert("RGB")
                im.thumbnail((480, 640))
                im.save(buf, "JPEG", quality=84)
                mime = "image/jpeg"
        uri = f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode()}"
        _cache[key] = (uri, hue)
    except Exception:
        _cache[key] = (None, None)
    return _cache[key]
