"""Plataformas: chave interna -> (apelidos, id no IGDB, id no ScreenScraper).
Os ids foram escritos de memória a partir das listas públicas das duas APIs; se alguma busca
por plataforma vier vazia, o scraper tenta de novo sem o filtro de plataforma."""
import re

PLATFORMS = {
    "pc": (("pc", "windows", "computador"), 6, 135),
    "ps1": (("ps1", "psx", "playstation 1", "playstation one", "duckstation"), 7, 57),
    "ps2": (("ps2", "playstation 2", "pcsx2"), 8, 58),
    "ps3": (("ps3", "playstation 3", "rpcs3"), 9, 59),
    "psp": (("psp", "ppsspp", "playstation portable"), 38, 61),
    "xbox": (("xbox", "xemu"), 11, 32),
    "x360": (("xbox 360", "x360", "xenia"), 12, 33),
    "switch": (("switch", "yuzu", "ryujinx", "suyu"), 130, 225),
    "wii": (("wii", "dolphin"), 5, 16),
    "gc": (("gamecube", "game cube", "dolphin"), 21, 13),
    "n64": (("n64", "nintendo 64", "mupen64"), 4, 14),
    "snes": (("snes", "super nintendo", "super nes"), 19, 4),
    "nes": (("nes", "nintendo entertainment system", "famicom"), 18, 3),
    "gba": (("gba", "game boy advance", "mgba"), 24, 12),
    "gbc": (("gbc", "game boy color"), 22, 10),
    "gb": (("game boy", "gameboy"), 33, 9),
    "nds": (("nds", "nintendo ds", "desmume", "melonds"), 20, 15),
    "3ds": (("3ds", "citra", "lime3ds"), 37, 17),
    "genesis": (("mega drive", "megadrive", "genesis"), 29, 1),
    "sms": (("master system",), 64, 2),
    "saturn": (("saturn",), 32, 22),
    "dc": (("dreamcast", "flycast"), 23, 23),
}
_IGDB = {v[1]: k for k, v in PLATFORMS.items()}
_SS = {v[2]: k for k, v in PLATFORMS.items()}
_ALIASES = sorted(((a, k) for k, v in PLATFORMS.items() for a in v[0]), key=lambda t: -len(t[0]))


def detect_platforms(*texts):
    """'PCSX2' / 'PlayStation 2' / 'Dolphin' -> ['ps2'] / ['ps2'] / ['wii', 'gc']. Apelidos longos primeiro."""
    keys = []
    for text in texts:
        s = " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "
        for alias, key in _ALIASES:
            pat = " " + alias + " "
            if pat in s:
                if key not in keys:
                    keys.append(key)
                s = s.replace(pat, " ")
    return keys


def igdb_id(key):
    return PLATFORMS[key][1] if key in PLATFORMS else None


def ss_id(key):
    return PLATFORMS[key][2] if key in PLATFORMS else None


def key_from_igdb(pid):
    return _IGDB.get(pid)


def key_from_ss(sid):
    try:
        return _SS.get(int(sid))
    except (TypeError, ValueError):
        return None
