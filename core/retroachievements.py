"""RetroAchievements (retroachievements.org) para os jogos retro que rodam pelo RetroBat.

Quem desbloqueia as conquistas é o EMULADOR (RetroArch, DuckStation, PCSX2, PPSSPP, Dolphin…), com a conta que o
RetroBat repassa. O NeoSD faz três coisas:
  1. Conta: grava no es_settings.cfg do RetroBat as chaves que o emulatorLauncher lê (confirmado no próprio
     emulatorLauncher.exe): global.retroachievements, .username, .password, .hardcore. A senha fica só lá, como
     faria o menu do RetroBat. Para LER conquistas o NeoSD usa a Web API do RA com usuário + chave pessoal
     (retroachievements.org/settings → "Keys"), que fica nas configurações do NeoSD.
  2. Identificar o jogo: calcula o "hash" da ROM do mesmo jeito que o rcheevos (biblioteca dos emuladores) e
     procura na lista pública de hashes do console (dorequest.php?r=hashlibrary, a mesma que o RetroBat usa).
     Funciona para cartuchos e fliperama. Jogos em disco (PS1, PS2, Saturn, Dreamcast…) usam regras de hash bem
     mais complexas: por enquanto ficam de fora, mas as conquistas deles continuam funcionando no emulador.
  3. Mostrar: lista de conquistas do jogo (com pontos e hardcore) e o aviso por cima do jogo. Para o aviso, enquanto
     um jogo do RetroBat aberto pelo NeoSD estiver rodando, consulta os desbloqueios recentes a cada 15 s.
"""
import calendar
import hashlib
import json
import logging
import os
import threading
import time
import zipfile
from pathlib import Path

log = logging.getLogger("neosd.ra")
SITE = "https://retroachievements.org"
API = SITE + "/API/"
CONNECT = SITE + "/dorequest.php"
MEDIA = "https://media.retroachievements.org"
UA = "NeoSD/1.5 (RetroBat frontend)"
HASHLIB_TTL, FAIL_TTL, GAME_TTL = 7 * 86400, 3600, 60
POLL_EVERY, AFTER_EXIT = 15, 45
MAX_ROM = 96 * 1024 * 1024

# sistema do RetroBat -> console do RetroAchievements (IDs públicos do RA)
CONSOLES = {
    "megadrive": 1, "genesis": 1, "n64": 2, "snes": 3, "sfc": 3, "gb": 4, "gba": 5, "gbc": 6, "nes": 7, "famicom": 7,
    "pcengine": 8, "tg16": 8, "segacd": 9, "megacd": 9, "sega32x": 10, "mastersystem": 11, "psx": 12, "lynx": 13,
    "ngp": 14, "ngpc": 14, "gamegear": 15, "gamecube": 16, "jaguar": 17, "nds": 18, "ps2": 21, "o2em": 23,
    "odyssey2": 23, "pokemini": 24, "atari2600": 25, "mame": 27, "fbneo": 27, "neogeo": 27, "cps1": 27, "cps2": 27,
    "cps3": 27, "arcade": 27, "virtualboy": 28, "msx": 29, "msx1": 29, "msx2": 29, "sg1000": 33, "amstradcpc": 37,
    "apple2": 38, "saturn": 39, "dreamcast": 40, "psp": 41, "3do": 43, "colecovision": 44, "intellivision": 45,
    "vectrex": 46, "pc88": 47, "pcfx": 49, "atari7800": 51, "wswan": 53, "wswanc": 53, "neogeocd": 56,
    "channelf": 57, "supervision": 63, "megaduck": 69, "arduboy": 71, "pcenginecd": 76, "jaguarcd": 77,
}
# consoles cujo hash é o MD5 do arquivo (com as exceções de cabeçalho tratadas em rom_hash)
CART = {1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 13, 14, 15, 17, 24, 25, 28, 29, 33, 44, 45, 46, 51, 53, 57, 63, 69}
ARCADE = 27
SKIP_IN_ZIP = (".txt", ".nfo", ".diz", ".xml", ".jpg", ".png", ".pdf")


# ---------- hash da ROM (regras do rcheevos para cartuchos) ----------
def _n64_bigendian(b):
    if not b:
        return b
    if b[0] == 0x80:  # .z64, já big-endian
        return b
    if b[0] == 0x37:  # .v64: troca cada par de bytes
        a = bytearray(b)
        a[0::2], a[1::2] = b[1::2], b[0::2]
        return bytes(a)
    if b[0] == 0x40:  # .n64: little-endian, inverte cada grupo de 4 bytes
        a = bytearray(len(b))
        for i in range(4):
            a[i::4] = b[3 - i::4]
        return bytes(a)
    return b


def data_hash(console, data):
    if console == 7 and data[:4] == b"NES\x1a":
        data = data[16:]
    elif console == 3 and len(data) % 0x2000 == 512:
        data = data[512:]
    elif console == 8 and len(data) % 0x20000 == 512:
        data = data[512:]
    elif console == 51 and data[1:10] == b"ATARI7800":
        data = data[128:]
    elif console == 13 and data[:5] == b"LYNX\0":
        data = data[64:]
    elif console == 2:
        data = _n64_bigendian(data)
    return hashlib.md5(data).hexdigest()


# ---------- discos de PlayStation (imagem .iso / .bin+.cue) ----------
class IsoReader:
    """Leitor mínimo de ISO9660: acha um arquivo pelo caminho e lê o conteúdo. Aceita setores de 2048 bytes (.iso)
    e imagens "cruas" de CD com 2352 bytes por setor (.bin de PS1, modo 1 ou modo 2)."""

    def __init__(self, path):
        self.f = open(path, "rb")
        self.size = os.path.getsize(path)
        self.sector, self.offset = None, 0
        for sec, off in ((2048, 0), (2352, 24), (2352, 16)):
            self.f.seek(16 * sec + off)
            if self.f.read(6) == b"\x01CD001":
                self.sector, self.offset = sec, off
                break
        if not self.sector:
            raise ValueError("imagem de disco sem sistema de arquivos ISO9660 (CHD e outros formatos não são lidos)")
        pvd = self.read_sector(16)
        self.root = (int.from_bytes(pvd[158:162], "little"), int.from_bytes(pvd[166:170], "little"))

    def close(self):
        self.f.close()

    def read_sector(self, n):
        self.f.seek(n * self.sector + self.offset)
        return self.f.read(2048)

    def read(self, lba, size, limit=64 * 1024 * 1024):
        out, n = bytearray(), lba
        while len(out) < min(size, limit):
            out += self.read_sector(n)
            n += 1
        return bytes(out[:min(size, limit)])

    def _entries(self, lba, size):
        for i in range((size + 2047) // 2048):
            sec, pos = self.read_sector(lba + i), 0
            while pos < len(sec) and sec[pos]:
                rec = sec[pos:pos + sec[pos]]
                name = rec[33:33 + rec[32]].decode("latin-1")
                yield name.split(";")[0].upper(), int.from_bytes(rec[2:6], "little"), int.from_bytes(rec[10:14], "little"), bool(rec[25] & 2)
                pos += sec[pos]

    def find(self, path):
        """-> (setor, tamanho) do arquivo ou None. Caminho como no SYSTEM.CNF: 'SLUS_210.87' ou 'PASTA\\ARQ.ELF'."""
        lba, size = self.root
        parts = [x for x in path.replace("/", "\\").split("\\") if x]
        for i, part in enumerate(parts):
            want = part.split(";")[0].upper()
            hit = next(((l, sz, d) for n, l, sz, d in self._entries(lba, size) if n == want), None)
            if not hit or (i < len(parts) - 1 and not hit[2]):
                return None
            lba, size = hit[0], hit[1]
        return lba, size


def _disc_image(path):
    """.cue -> o .bin da primeira trilha; .iso/.bin/.img -> o próprio arquivo."""
    p = Path(path)
    if p.suffix.lower() == ".cue":
        for line in p.read_text(encoding="latin-1", errors="replace").splitlines():
            line = line.strip()
            if line.upper().startswith("FILE"):
                name = line[4:].strip()
                name = name[1:name.index('"', 1)] if name.startswith('"') else name.split()[0]
                return p.parent / name
    return p


def playstation_hashes(console, path):
    """Hash de disco no padrão do rcheevos para PS1/PS2/PSP. A regra principal (PS1/PS2: nome do executável indicado
    no SYSTEM.CNF + conteúdo dele) vem primeiro; variações vêm depois e só valem se estiverem na lista oficial."""
    p = _disc_image(path)
    if p.suffix.lower() in (".chd", ".cso", ".zso", ".gz", ".pbp", ".7z", ".rar", ".zip"):
        return [], f"formato {p.suffix.lower()} (converta para .iso, ou .bin/.cue no PS1)"
    iso = IsoReader(p)
    try:
        if console == 41:  # PSP
            out = []
            sfo, ebt = iso.find("PSP_GAME\\PARAM.SFO"), iso.find("PSP_GAME\\SYSDIR\\EBOOT.BIN")
            if sfo and ebt:
                out.append(hashlib.md5(iso.read(*sfo) + iso.read(*ebt)).hexdigest())
            return out, "" if out else "não achei o PSP_GAME no disco"
        return _ps_hashes(console, lambda name: (lambda loc: iso.read(*loc) if loc else None)(iso.find(name)))
    finally:
        iso.close()


def _ps_hashes(console, read):
    """PS1/PS2: read(nome) -> bytes do arquivo no disco (ou None). Igual para imagem .iso/.bin e para o disco no leitor."""
    cnf = read("SYSTEM.CNF")
    key = "BOOT2" if console == 21 else "BOOT"
    exe = None
    if cnf:
        for line in cnf.decode("latin-1", "replace").splitlines():
            k, _, v = line.partition("=")
            if k.strip().upper() == key:
                v = v.strip()
                v = v.split(":", 1)[1] if ":" in v else v  # tira "cdrom0:" / "cdrom:"
                exe = v.lstrip("\\/").split(";")[0].strip()
                break
    if not exe and console == 12 and read("PSX.EXE") is not None:
        exe = "PSX.EXE"
    data = read(exe) if exe else None
    if data is None:
        return [], "não achei o executável do jogo no disco"
    names = [exe, exe + ";1"]
    out = []
    if console == 12 and data[:8] == b"PS-X EXE":  # PS1: tamanho do executável vem do cabeçalho
        body = data[:int.from_bytes(data[28:32], "little") + 2048]
        out += [hashlib.md5(n.encode("latin-1") + body).hexdigest() for n in names]
    out += [hashlib.md5(n.encode("latin-1") + data).hexdigest() for n in names]
    out.append(hashlib.md5(data).hexdigest())
    return list(dict.fromkeys(out)), ""


def drive_hashes(console, drive, limit=64 * 1024 * 1024):
    """Hash do disco FÍSICO no leitor (D:\\): o Windows já mostra os arquivos do disco, então lê o SYSTEM.CNF e o
    executável direto, sem copiar o disco. -> ([hashes], motivo)."""
    if console not in (12, 21):
        return [], "sistema sem leitura de disco"

    def read(name):
        p = Path(drive, *[x for x in name.replace("/", "\\").split("\\") if x])
        try:
            with open(p, "rb") as f:
                return f.read(limit)
        except OSError:
            return None
    try:
        return _ps_hashes(console, read)
    except OSError as e:
        return [], str(e)


DISC = {12, 21, 41}


def rom_hashes(console, path):
    """-> ([hashes candidatos], motivo). Cartuchos têm um só; discos podem ter variações."""
    if str(path).lower().endswith(".m3u"):  # jogo de vários discos: o disco 1 identifica o jogo
        from .multidisc import read_m3u
        first = next(iter(read_m3u(path)), "")
        if not first:
            return [], "lista .m3u vazia"
        path = first
    if console in DISC:
        try:
            return playstation_hashes(console, path)
        except (OSError, ValueError) as e:
            return [], str(e)
    h, why = rom_hash(console, path)
    return ([h] if h else []), why


def rom_hash(console, path):
    """-> (hash md5 | None, motivo)."""
    p = Path(path)
    if console == ARCADE:  # fliperama: o hash é o nome do arquivo, sem extensão
        return hashlib.md5(p.stem.encode("utf-8")).hexdigest(), ""
    if console not in CART:
        return None, "disco"
    try:
        if p.suffix.lower() == ".zip":
            with zipfile.ZipFile(p) as z:
                names = [n for n in z.namelist() if not n.endswith("/") and not n.lower().endswith(SKIP_IN_ZIP)]
                if not names:
                    return None, "zip vazio"
                info = z.getinfo(names[0])
                if info.file_size > MAX_ROM:
                    return None, "grande demais"
                return data_hash(console, z.read(names[0])), ""
        if p.suffix.lower() in (".7z", ".rar"):
            return None, "compactado em 7z/rar"
        if p.stat().st_size > MAX_ROM:
            return None, "grande demais"
        return data_hash(console, p.read_bytes()), ""
    except (OSError, zipfile.BadZipFile) as e:
        return None, str(e)


def _utc(s):
    try:
        return calendar.timegm(time.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return 0


class RetroAchievementsService:
    def __init__(self, settings, retrobat, data_dir, http=None, now=time.time):
        self.settings, self.rb, self.dir = settings, retrobat, Path(data_dir)
        self._http, self.now = http, now
        self._lock = threading.Lock()
        self._index = None
        self._hashlib = {}
        self._games = {}
        self._session = None
        self.announce = lambda items: None  # ligado no main (mesmo aviso das conquistas da Steam)

    # ---------- conta ----------
    def user(self):
        return str(self.settings.get("ra_user", "") or "")

    def key(self):
        return str(self.settings.get("ra_key", "") or "")

    def configured(self):
        return bool(self.user() and self.key())

    def status(self):
        es = self.rb.es_get_all() if self.rb and self.rb.root() else {}
        on = str(es.get("global.retroachievements", "")).lower() in ("true", "1")
        return {"configured": self.configured(), "user": self.user(), "has_key": bool(self.key()),
                "retrobat": bool(self.rb and self.rb.root()), "enabled": on,
                "has_password": bool(es.get("global.retroachievements.password")),
                "hardcore": str(es.get("global.retroachievements.hardcore", "")).lower() in ("true", "1"),
                "rb_user": es.get("global.retroachievements.username", "")}

    def configure(self, user=None, password=None, key=None, hardcore=None, enabled=None):
        """Salva a conta: usuário/chave no NeoSD; usuário/senha/hardcore no RetroBat (para os emuladores)."""
        if user is not None:
            self.settings.set("ra_user", user.strip())
        if key is not None and key.strip():
            self.settings.set("ra_key", key.strip())
        changes = {}
        if self.rb and self.rb.root():
            if user is not None:
                changes["global.retroachievements.username"] = user.strip() or None
            if password:
                changes["global.retroachievements.password"] = password
            if hardcore is not None:
                changes["global.retroachievements.hardcore"] = bool(hardcore)
            if enabled is not None:
                changes["global.retroachievements"] = bool(enabled)
            elif user:
                changes["global.retroachievements"] = True
            if changes:
                self.rb.es_set(changes)
        self._games.clear()
        return self.status()

    # ---------- rede ----------
    def _get(self, url, params=None, timeout=12, method="get", data=None):
        http = self._http
        if http is None:
            import requests as http
        headers = {"User-Agent": UA}
        r = http.post(url, data=data, timeout=timeout, headers=headers) if method == "post" else \
            http.get(url, params=params, timeout=timeout, headers=headers)
        r.raise_for_status()
        return r.json()

    def _api(self, name, **params):
        params.update(y=self.key())
        if "u" not in params and self.user():
            params["u"] = self.user()
        return self._get(API + f"API_{name}.php", params)

    def test(self, password=None):
        """Confere a chave (perfil pela Web API) e, se veio, a senha (login dos emuladores)."""
        out = {"ok": False, "msg": ""}
        if not self.configured():
            out["msg"] = "Preencha o usuário e a chave da Web API."
            return out
        try:
            p = self._api("GetUserProfile")
        except Exception as e:
            log.info("RA perfil: %s", e)
            out["msg"] = "A chave ou o usuário não foram aceitos (ou sem internet). Confira em retroachievements.org/settings."
            return out
        if not isinstance(p, dict) or not p.get("User"):
            out["msg"] = "Usuário não encontrado no RetroAchievements."
            return out
        out.update(ok=True, user=p.get("User"), points=p.get("TotalPoints", 0), softcore=p.get("TotalSoftcorePoints", 0),
                   pic=MEDIA + p["UserPic"] if p.get("UserPic") else "", msg="")
        if password:
            try:
                r = self._get(CONNECT, method="post", data={"r": "login2", "u": self.user(), "p": password})
                out["password_ok"] = bool(r.get("Success"))
            except Exception as e:
                log.info("RA login: %s", e)
                out["password_ok"] = None
        return out

    # ---------- identificar o jogo ----------
    def _index_file(self):
        return self.dir / "ra_index.json"

    def _load_index(self):
        if self._index is None:
            try:
                self._index = json.loads(self._index_file().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._index = {}
        return self._index

    def _save_index(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self._index_file().with_suffix(".tmp")
            tmp.write_text(json.dumps(self._index), encoding="utf-8")
            os.replace(tmp, self._index_file())
        except OSError:
            pass

    def hashlib(self, console):
        """{md5: id do jogo} de um console (lista pública; guardada 7 dias)."""
        hit = self._hashlib.get(console)
        f = self.dir / "ra_hashlib" / f"{console}.json"
        if hit is None and f.is_file():
            try:
                hit = self._hashlib[console] = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                hit = None
        if hit and self.now() - hit.get("t", 0) < (HASHLIB_TTL if hit.get("map") else FAIL_TTL):
            return hit["map"]
        try:
            r = self._get(CONNECT, {"r": "hashlibrary", "c": console}, timeout=30)
            m = {k.lower(): int(v) for k, v in (r.get("MD5List") or {}).items()}
        except Exception as e:
            log.info("lista de hashes do console %s indisponível: %s", console, e)
            m = (hit or {}).get("map") or {}
        hit = self._hashlib[console] = {"t": self.now(), "map": m}
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(hit), encoding="utf-8")
        except OSError:
            pass
        return m

    def lookup(self, console, hashes):
        """-> id do jogo no RA para algum dos hashes (lista oficial), ou None."""
        if not hashes:
            return None
        lib = self.hashlib(console)
        return next((lib[h] for h in hashes if h in lib), None)

    def disc_id(self, system, drive):
        """Disco físico no leitor -> (hashes, id no RA | None)."""
        console = CONSOLES.get(system)
        if not console:
            return [], None
        hs, _ = drive_hashes(console, drive)
        try:
            return hs, self.lookup(console, hs)
        except Exception as e:
            log.info("RA disco: %s", e)
            return hs, None

    def game_id(self, game):
        """-> (id do jogo no RA | None, motivo para mostrar)."""
        if game.get("launch_type") == "disc":  # disco físico: hashes guardados quando o disco entrou no leitor
            console = CONSOLES.get(game.get("disc_system", ""))
            if not console:
                return None, "Este sistema não tem conquistas no RetroAchievements."
            hs = [h for d in game.get("disc_set") or [] for h in d.get("ra_hashes") or []]
            if not hs:
                return None, "Coloque o disco no leitor para o NeoSD identificar o jogo."
            gid = self.lookup(console, hs)
            return (gid, "") if gid else (None, "Este disco não está na lista do RetroAchievements (versão ou região "
                                                "ainda sem conquistas).")
        system, rom = game.get("rb_system", ""), game.get("rom_path", "")
        console = CONSOLES.get(system)
        if not console:
            return None, "Este sistema não tem conquistas no RetroAchievements."
        if not rom or not os.path.isfile(rom):
            return None, "ROM não encontrada."
        st = os.stat(rom)
        with self._lock:
            idx = self._load_index()
            e = idx.get(rom)
            if not e or e.get("size") != st.st_size or e.get("mtime") != int(st.st_mtime) or "hashes" not in e:
                hs, why = rom_hashes(console, rom)
                e = idx[rom] = {"size": st.st_size, "mtime": int(st.st_mtime), "hashes": hs, "why": why, "console": console}
                self._save_index()
        if not e.get("hashes"):
            why = e.get("why", "")
            return None, ("Jogos em disco deste sistema ainda não são identificados pelo NeoSD; as conquistas continuam "
                          "funcionando no emulador." if why == "disco" else
                          "ROMs em .7z/.rar não são identificadas; use .zip ou o arquivo solto." if why == "compactado em 7z/rar" else
                          f"Não consegui identificar o jogo: {why}.")
        lib = self.hashlib(console)
        gid = next((lib[h] for h in e["hashes"] if h in lib), None)
        if not gid:
            return None, ("Esta versão da ROM não é reconhecida pelo RetroAchievements: o site só aceita as versões "
                          "cadastradas para cada jogo (em geral a versão “No-Intro”).")
        return gid, ""

    # ---------- conquistas de um jogo ----------
    def game(self, game, fresh=False):
        if not self.configured():
            return {"ok": False, "reason": "Entre com a sua conta do RetroAchievements em Configurações → Emulação."}
        gid, why = self.game_id(game)
        if not gid:
            return {"ok": False, "reason": why}
        hit = self._games.get(gid)
        if hit and not fresh and self.now() - hit[0] < GAME_TTL:
            return hit[1]
        try:
            d = self._api("GetGameInfoAndUserProgress", g=gid)
        except Exception as e:
            log.info("RA jogo %s: %s", gid, e)
            return {"ok": False, "reason": "Não consegui falar com o RetroAchievements (internet ou chave)."}
        players = max(1, int(d.get("NumDistinctPlayers") or d.get("NumDistinctPlayersCasual") or 1))
        items = []
        for a in sorted((d.get("Achievements") or {}).values(), key=lambda a: (a.get("DisplayOrder", 0), a.get("ID", 0))):
            hc, sc = a.get("DateEarnedHardcore"), a.get("DateEarned")
            got = bool(hc or sc)
            badge = str(a.get("BadgeName") or "")
            items.append({"key": str(a.get("ID")), "name": a.get("Title", ""), "desc": a.get("Description", ""),
                          "hidden": False, "unlocked": got, "time": _utc(hc or sc), "hardcore": bool(hc),
                          "points": int(a.get("Points") or 0), "type": a.get("type") or "",
                          "icon": f"{MEDIA}/Badge/{badge}{'' if got else '_lock'}.png" if badge else "",
                          "rarity": round(100 * int(a.get("NumAwarded") or 0) / players, 1)})
        n = sum(1 for i in items if i["unlocked"])
        out = {"ok": True, "source": "ra", "ra_id": gid, "title": d.get("Title", ""), "console": d.get("ConsoleName", ""),
               "total": len(items), "unlocked": n, "synced": True, "items": items,
               "points": sum(i["points"] for i in items), "points_user": sum(i["points"] for i in items if i["unlocked"]),
               "award": d.get("HighestAwardKind") or "", "icon": MEDIA + d["ImageIcon"] if d.get("ImageIcon") else ""}
        if not items:
            out = {"ok": False, "reason": "Este jogo ainda não tem conquistas no RetroAchievements."}
        self._games[gid] = (self.now(), out)
        return out

    def summary(self, game):
        g = self.game(game)
        return {"ok": g.get("ok", False), "total": g.get("total", 0), "unlocked": g.get("unlocked", 0),
                "source": "ra", "reason": g.get("reason", "")}

    # ---------- aviso por cima do jogo ----------
    def recent(self, minutes=60):
        r = self._api("GetUserRecentAchievements", m=minutes)
        return r if isinstance(r, list) else []

    def on_launch(self, system, rom, proc):
        """Chamado quando o NeoSD abre um jogo do RetroBat: acompanha os desbloqueios enquanto ele roda."""
        if not self.configured() or not self.settings.get("ach_overlay", True):
            return
        if self._session and self._session.is_alive():
            self._session.proc = proc
            return
        t = threading.Thread(target=self._watch, args=(proc,), daemon=True, name="ra-watch")
        t.proc = proc
        self._session = t
        t.start()

    def _watch(self, proc):
        me = threading.current_thread()
        seen, ended = None, None
        log.info("RetroAchievements: acompanhando o jogo")
        while True:
            try:
                rec = self.recent(minutes=120)
                ids = {(str(a.get("AchievementID")), str(a.get("Date"))) for a in rec}
                if seen is None:
                    seen = ids  # o que já estava desbloqueado antes de abrir o jogo não é avisado
                else:
                    new = [a for a in rec if (str(a.get("AchievementID")), str(a.get("Date"))) not in seen]
                    seen |= ids
                    if new:
                        self._games.clear()
                        self.announce([{"name": a.get("Title", ""), "desc": a.get("Description", ""),
                                        "game": a.get("GameTitle", ""), "time": _utc(a.get("Date")),
                                        "icon": MEDIA + a["BadgeURL"] if a.get("BadgeURL") else
                                        (f"{MEDIA}/Badge/{a.get('BadgeName')}.png" if a.get("BadgeName") else ""),
                                        "ikey": f"ra_{a.get('BadgeName', '')}", "hardcore": bool(a.get("HardcoreMode"))}
                                       for a in sorted(new, key=lambda a: str(a.get("Date")))])
            except Exception as e:
                log.info("RetroAchievements (recentes): %s", e)
            p = getattr(me, "proc", proc)
            if p is not None and p.poll() is not None:
                ended = ended or self.now()
                if self.now() - ended > AFTER_EXIT:  # um pouco depois de fechar, para pegar o último envio
                    log.info("RetroAchievements: jogo fechado, parei de acompanhar")
                    return
            time.sleep(POLL_EVERY)
