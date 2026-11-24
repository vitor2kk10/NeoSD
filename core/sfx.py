"""Efeitos sonoros do modo console.

Os sons ficam na pasta SFX (ao lado do NeoSD.exe ou em %LOCALAPPDATA%\\NeoSD\\SFX) e são reconhecidos pelo NOME
do arquivo, então dá para trocar por outros sem mexer no código. Ex.: "17. Focus Move.mp3" ou só "move.mp3".

Por que tocar pelo Python e não pela página: o WebView2 bloqueia áudio que começa sem um clique, e apertar o
controle não conta como clique. Aqui usamos o MCI do Windows (winmm.dll), que toca MP3/WAV/WMA sem essa
restrição; cada som é aberto uma vez e reaproveitado, para responder rápido na navegação.
"""
import ctypes
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("neosd.sfx")
EXTS = (".mp3", ".wav", ".wma")
# ordem importa: o primeiro padrão que bater leva o arquivo ("open home" antes de "enter"/"ok" etc.)
KEYS = (
    ("intro", ("open home", "home", "intro", "startup", "boot", "abertura")),
    ("trophy", ("trophy", "achievement", "conquista", "trofeu", "troféu")),
    ("move", ("focus move", "move", "cursor", "naveg")),
    ("tab", ("change pan", "panel", "tab", "aba")),
    ("backspace", ("backspace", "delete", "apagar")),  # antes de "back", que está dentro de "backspace"
    ("back", ("cancel", "back", "voltar")),
    ("confirm", ("yes", "confirm", "jogar")),
    ("enter", ("enter", "select", "ok")),
    ("error", ("error", "erro")),
)
POOL = {"move": 6, "tab": 3, "enter": 3, "back": 3, "confirm": 2, "error": 3, "backspace": 4, "intro": 1, "trophy": 2}
NAMES = {"intro": "Abrir o modo console", "trophy": "Conquista", "move": "Mover o foco", "tab": "Trocar de aba",
         "back": "Voltar / cancelar", "confirm": "Confirmar / jogar", "enter": "Abrir / selecionar",
         "backspace": "Apagar no teclado", "error": "Erro"}


def _norm(stem):
    s = re.sub(r"^\s*\d+[\s.\-_]*", "", stem.lower())  # "17. Focus Move" -> "focus move"
    return re.sub(r"[\s_\-]+", " ", s).strip()


def match(files):
    """{chave: caminho} a partir de uma lista de arquivos de som."""
    out, rest = {}, []
    for f in files:
        n = _norm(Path(f).stem)
        if n in dict(KEYS) and n not in out:  # nome exato da chave ("move.mp3")
            out[n] = f
        else:
            rest.append((n, f))
    for key, pats in KEYS:
        if key in out:
            continue
        for n, f in rest:
            if f not in out.values() and any(p in n for p in pats):
                out[key] = f
                break
    return out


class SfxService:
    def __init__(self, folders, settings):
        self.folders = [Path(f) for f in folders]
        self.settings = settings
        self._map, self._sig = {}, None
        self._q = queue.Queue()
        self._thread = None

    # ---------- arquivos ----------
    def files(self):
        seen, out = set(), []
        for d in self.folders:
            if d.is_dir():
                for f in sorted(d.iterdir()):
                    if f.suffix.lower() in EXTS and f.name.lower() not in seen:
                        seen.add(f.name.lower())
                        out.append(f)
        return out

    def sounds(self):
        files = self.files()
        sig = tuple((str(f), f.stat().st_mtime) for f in files)
        if sig != self._sig:
            self._sig, self._map = sig, match(files)
            log.info("sons: %s", {k: v.name for k, v in self._map.items()})
        return self._map

    def status(self):
        m = self.sounds()
        folder = next((d for d in self.folders if d.is_dir()), self.folders[0] if self.folders else None)
        return {"enabled": bool(self.settings.get("sfx_on", True)), "volume": int(self.settings.get("sfx_vol", 80)),
                "intro": bool(self.settings.get("console_intro", True)), "folder": str(folder or ""),
                "sounds": [{"key": k, "name": NAMES[k], "file": m[k].name if k in m else ""} for k, _ in KEYS]}

    def has(self, key):
        return key in self.sounds()

    # ---------- tocar ----------
    def _ensure_thread(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True, name="sfx")
            self._thread.start()

    def play(self, key):
        """Toca na thread de áudio. Conquista segue a opção 'Som do aviso'; o resto, 'Sons do modo console'."""
        if os.name != "nt":
            return False
        on = self.settings.get("ach_sound", True) if key == "trophy" else self.settings.get("sfx_on", True)
        path = self.sounds().get(key)
        if not on or not path:
            return False
        self._ensure_thread()
        self._q.put(("play", key, str(path)))
        return True

    def preload(self):
        """Abre os tocadores dos sons de navegação antes do primeiro uso (o primeiro 'open' do MCI é lento)."""
        if os.name == "nt" and self.settings.get("sfx_on", True):
            for key in ("move", "tab", "enter", "back", "confirm", "error"):
                if key in self.sounds():
                    self._ensure_thread()
                    self._q.put(("load", key, str(self._map[key])))

    def _run(self):
        """Cada som tem vários tocadores (POOL) usados em rodízio: apertar de novo usa outro tocador e o som
        anterior termina normalmente, em vez de ser cortado e reiniciado (o que soava como corte e salto de volume)."""
        from ctypes import wintypes
        mci = ctypes.windll.winmm.mciSendStringW
        mci.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        opened, vols, turn = {}, {}, {}  # alias -> arquivo; alias -> volume aplicado; som -> próximo tocador

        def send(cmd):
            err = mci(cmd, None, 0, None)
            if err:
                buf = ctypes.create_unicode_buffer(256)
                ctypes.windll.winmm.mciGetErrorStringW(err, buf, 256)
                log.warning("som: %s -> %s", cmd[:90], buf.value)
            return err

        def voice(key, path, i):
            alias = f"neosd_{key}_{i}"
            if opened.get(alias) != path:
                if alias in opened:
                    send(f"close {alias}")
                    opened.pop(alias, None)
                if send(f'open "{path}" type mpegvideo alias {alias}'):
                    return None
                opened[alias] = path
                vols.pop(alias, None)
            vol = max(0, min(100, int(self.settings.get("sfx_vol", 80)))) * 10
            if vols.get(alias) != vol:
                send(f"setaudio {alias} volume to {vol}")
                vols[alias] = vol
            return alias

        while True:
            try:
                batch = [self._q.get(timeout=0.02)]
            except queue.Empty:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # o MCI usa mensagens do Windows
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                continue
            while True:  # junta o que chegou de uma vez: no máximo 2 toques iguais por rodada
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            count = {}
            for op, key, path in batch:
                try:
                    n = POOL.get(key, 2)
                    if op == "load":
                        for i in range(n):
                            voice(key, path, i)
                        continue
                    count[key] = count.get(key, 0) + 1
                    if count[key] > 2:
                        continue
                    i = turn.get(key, 0)
                    turn[key] = (i + 1) % n
                    alias = voice(key, path, i)
                    if alias:
                        send(f"play {alias} from 0")
                except Exception:
                    log.exception("tocar som %s", key)
