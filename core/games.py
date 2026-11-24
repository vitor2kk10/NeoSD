"""Catálogo, emuladores, regras de 'jogo ativo', importação de cartucho/ROMs e lançamento."""
import ntpath
import os
import re
import shlex
import shutil
import subprocess
import threading
from pathlib import Path

from PIL import Image

from . import sd
from .media import hash_hue, load_art
from .store import LIBRARY, load_json, resolve, save_json

METADATA_KEYS = ("platform", "genre", "developer", "release_date", "size", "playtime")
DEFAULT_ROM_EXTS = ("iso", "bin", "cue", "chd", "cso", "gcm", "rvz", "wbfs", "nsp", "xci", "nes", "sfc", "smc",
                    "gba", "gb", "gbc", "nds", "3ds", "n64", "z64", "v64", "md", "gen", "sms", "gg", "pce",
                    "zip", "7z", "rom", "elf")


def safe_name(name):
    return "".join(c for c in name if c.isalnum() or c in " -_").strip() or "jogo"


def clean_rom_name(stem):
    """'God_of_War (USA) [!]' -> 'God of War'."""
    s = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", stem).replace("_", " ")
    return re.sub(r"\s+", " ", s).strip() or stem


def parse_exts(text):
    return [e.strip().lstrip("*.") for e in re.split(r"[,;\s]+", (text or "").lower()) if e.strip().lstrip("*.")]


def sd_identity(path, mounted):
    """Se `path` está na unidade de um cartão inserido agora, devolve a identidade dele (nº de série + nome.txt)."""
    drive = ntpath.splitdrive(path or "")[0].upper()
    for m in mounted:
        if drive and ntpath.splitdrive(m["drive"])[0].upper() == drive:
            return {"sd_serial": m["serial"], "sd_name": m["name"]}
    return None


def _norm(p):
    return os.path.normcase(os.path.normpath(p))


class GameService:
    def __init__(self, mounted=None):
        self.lock = threading.RLock()
        self.mounted = mounted or sd.mounted_media   # injetável nos testes
        self.catalog = load_json("catalog.json")
        self.emulators = load_json("emulators.json")
        LIBRARY.mkdir(exist_ok=True)

    # ---------- identificação do cartão ----------
    def source(self, game):
        """'sd' ou 'local' (cadastros antigos sem a chave: some o exe -> provavelmente SD)."""
        if game.get("source") in ("sd", "local", "disc"):
            return game["source"]
        exe = game.get("exe_path", "")
        return "local" if exe and os.path.exists(exe) else "sd"

    def _match_cart(self, game, mounted):
        """Qual cartão inserido É o deste jogo (ou None). Não olha a letra da unidade, só a identidade."""
        serial = game.get("sd_serial")
        if serial is not None:
            for m in mounted:
                same_name = not game.get("sd_name") or not m["name"] or m["name"] == game["sd_name"]
                if m["serial"] == serial and same_name:
                    return m
            return None
        # cadastro antigo (sem nº de série): só vale se o nome.txt do cartão bate com o jogo
        names = {game.get("name"), game.get("sd_name")} - {None, ""}
        return next((m for m in mounted if m["name"] and m["name"] in names), None)

    def _paths(self, game, mounted):
        """Caminhos prontos pra rodar agora ({'exe'} ou {'emu','rom'}), ou None se o jogo não está disponível.
        Em jogos de SD o caminho é refeito com a letra ATUAL do cartão certo."""
        if game.get("launch_type") == "disc":  # disco físico no leitor de CD/DVD
            discs = getattr(self, "discs", None)
            drive = discs.find(game) if discs else None
            if not drive or not discs.command(game.get("disc_system", ""), drive):
                return None
            return {"disc": discs, "drive": drive, "system": game.get("disc_system", "")}
        if game.get("launch_type") == "uri":  # Steam/Epic: abre pelo protocolo da loja; ativo enquanto estiver instalado
            inst = game.get("install_path", "")
            return {"uri": game["launch_uri"]} if game.get("launch_uri") and (not inst or os.path.isdir(inst)) else None
        emu_mode = game.get("launch_type") in ("emulator", "retrobat")
        exe, rom = game.get("exe_path", ""), game.get("rom_path", "")
        if self.source(game) == "sd":
            cart = self._match_cart(game, mounted)
            if not cart:
                return None
            def on_card(path):
                rel = ntpath.splitdrive(path)[1].lstrip("\\/")
                return os.path.join(cart["drive"], rel) if rel else ""
            exe, rom = (exe, on_card(rom)) if emu_mode else (on_card(exe), rom)
        if game.get("launch_type") == "retrobat":  # RetroBat como motor: emulatorLauncher escolhe e configura tudo
            rb = getattr(self, "retrobat", None)
            if not rb or not rb.root() or not rom or not os.path.exists(rom):
                return None
            return {"rb": rb, "rom": rom}
        if emu_mode:
            emu = self.emulators.get(game.get("emulator_id", ""))
            if not emu or not os.path.exists(emu.get("executable", "")) or not rom or not os.path.exists(rom):
                return None
            return {"emu": emu, "rom": rom}
        return {"exe": exe} if exe and os.path.exists(exe) else None

    def is_active(self, game, mounted=None):
        return self._paths(game, self.mounted() if mounted is None else mounted) is not None

    def _why_inactive(self, game, mounted):
        name = game.get("name", "")
        if game.get("launch_type") == "uri":
            return f"A pasta de {name} não foi encontrada. O jogo foi desinstalado?"
        if game.get("launch_type") == "disc":
            discs = getattr(self, "discs", None)
            drive = discs.find(game) if discs else None
            if drive:
                return discs.current.get(drive, {}).get("why") or "O emulador deste disco não está instalado no RetroBat."
            from .discs import optical_drives
            return f"Coloque o disco de {name} no leitor." if optical_drives() else "Este PC não tem leitor de CD/DVD."
        if self.source(game) == "sd" and not self._match_cart(game, mounted):
            return f"O cartão inserido não é o de {name}." if mounted else f"Insira o cartão SD de {name} para jogar."
        if game.get("launch_type") == "retrobat":
            rb = getattr(self, "retrobat", None)
            return "RetroBat não encontrado. Indique a pasta em Configurações → Bibliotecas." if not rb or not rb.root() else "ROM/ISO não encontrada."
        return "Emulador ou ROM/ISO não encontrados." if game.get("launch_type") == "emulator" else "Executável não encontrado."

    # ---------- lançamento ----------
    def launch(self, game_id):
        with self.lock:
            game = self.catalog.get(game_id)
        if not game:
            return False, "Jogo não encontrado."
        mounted = self.mounted()
        paths = self._paths(game, mounted)
        if not paths:
            return False, self._why_inactive(game, mounted)
        try:
            if "uri" in paths:
                start = getattr(os, "startfile", None)
                if start is None:
                    return False, "Abrir jogos de loja só funciona no Windows."
                start(paths["uri"])
            elif "disc" in paths:
                ok, msg = paths["disc"].launch(paths["system"], paths["drive"])
                if not ok:
                    return False, msg
            elif "rb" in paths:
                ovr = {"emulator": game.get("rb_emulator", ""), "core": game.get("rb_core", "")}
                ok, msg = paths["rb"].launch(game.get("rb_system", ""), paths["rom"], ovr)
                if not ok:
                    return False, msg
            elif "emu" in paths:
                emu = paths["emu"]
                template = emu.get("args_template") or '"{rom}"'
                args = shlex.split(template.format(rom=paths["rom"], core=emu.get("core", "")), posix=False)
                subprocess.Popen([emu["executable"]] + args, cwd=os.path.dirname(emu["executable"]))
            else:
                subprocess.Popen(paths["exe"], cwd=os.path.dirname(paths["exe"]))
            return True, ""
        except Exception as e:
            return False, f"Erro ao iniciar o jogo: {e}"

    # ---------- cartucho ----------
    def import_cartridge(self, cart):
        """Registra (ou atualiza) o jogo de um cartão e grava a identidade dele. Retorna (nome, é_novo)."""
        name = cart["name"]
        safe = safe_name(name)
        folder = LIBRARY / safe
        folder.mkdir(parents=True, exist_ok=True)
        local = {}
        for key in ("cover", "icon"):
            if cart.get(key):
                try:
                    dest = folder / os.path.basename(cart[key])
                    shutil.copy2(cart[key], dest)
                    local[key] = os.path.join("library", safe, dest.name)
                except OSError:
                    local[key] = None
        identity = {"sd_serial": cart.get("serial"), "sd_name": cart.get("txt_name", "")}
        with self.lock:
            is_new = name not in self.catalog
            if is_new:
                entry = {"name": name, "exe_path": cart["exe"], "description": cart["description"],
                         "cover": local.get("cover"), "icon": local.get("icon"),
                         "source": "sd", "drive": cart["drive"], **identity}
                entry.update({k: "" for k in METADATA_KEYS})
                self.catalog[name] = entry
            else:  # já catalogado (inclusive cadastro antigo): atualiza e passa a ter identidade
                self.catalog[name].update({"exe_path": cart["exe"], "drive": cart["drive"], "source": "sd", **identity})
            save_json("catalog.json", self.catalog)
        return name, is_new

    def import_disc(self, info):
        """Registra (ou reencontra) o jogo de um disco físico. Retorna (id, nome, é_novo)."""
        from .discs import SYSTEMS
        with self.lock:
            for key, g in self.catalog.items():
                if g.get("launch_type") == "disc" and (
                        (info["serial"] and g.get("disc_serial") == info["serial"]) or
                        (not info["serial"] and g.get("disc_vserial") == info["vserial"])):
                    g.update(disc_label=info["label"], disc_vserial=info["vserial"])
                    save_json("catalog.json", self.catalog)
                    return key, g.get("name", key), False
            name = info["name"]
            if name in self.catalog:  # o mesmo nome já existe (ex.: a ISO do mesmo jogo no RetroBat)
                name = f"{name} (disco)"
            entry = {"name": name, "launch_type": "disc", "source": "disc", "description": "", "cover": None, "icon": None,
                     "disc_system": info["system"], "disc_serial": info["serial"], "disc_vserial": info["vserial"],
                     "disc_label": info["label"], "platform": SYSTEMS.get(info["system"], "")}
            entry.update({k: "" for k in METADATA_KEYS if k not in entry})
            self.catalog[name] = entry
            save_json("catalog.json", self.catalog)
        return name, name, True

    # ---------- adicionar / editar / remover jogo ----------
    def get_form(self, game_id=None):
        with self.lock:
            g = dict(self.catalog.get(game_id) or {}) if game_id else {}
        form = {k: g.get(k) or "" for k in ("name", "description", "cover", "icon", "exe_path", "emulator_id",
                                           "rom_path", "launch_uri", "install_path") + METADATA_KEYS}
        form["launch_type"] = g.get("launch_type") if g.get("launch_type") in ("emulator", "uri", "retrobat", "disc") else "direct"
        form["rating"] = "" if g.get("rating") in (None, "") else str(g["rating"])
        form["source"] = self.source(g) if g else "local"
        form["cover_preview"] = load_art(resolve(form["cover"]), "cover")[0] or ""
        form["icon_preview"] = load_art(resolve(form["icon"]), "icon")[0] or ""
        emulators = [{"id": i, "name": e.get("name", i)} for i, e in self.emulators.items()]
        return {"form": form, "emulators": emulators}

    def _adopt_art(self, path, safe, role):
        """Capa/ícone escolhidos viram cópia em library/<jogo>/ (não somem se o original for movido/estiver no SD)."""
        path = (path or "").strip()
        if not path:
            return None
        src = resolve(path)
        if not src or not src.is_file():
            return path
        try:
            if LIBRARY.resolve() in src.resolve().parents:
                return path
            dest_dir = LIBRARY / safe
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{role}{src.suffix.lower()}"
            shutil.copy2(src, dest)
            return f"library/{safe}/{dest.name}"
        except OSError:
            return path

    def save_game(self, form, original_id=None):
        """Cria (original_id=None) ou atualiza um jogo. Retorna (ok, mensagem, id)."""
        name = (form.get("name") or "").strip()
        if not name:
            return False, "Preencha o nome do jogo.", None
        editing = bool(original_id)
        launch = form.get("launch_type") if form.get("launch_type") in ("emulator", "uri", "retrobat", "disc") else "direct"
        with self.lock:
            was_rb = editing and (self.catalog.get(original_id) or {}).get("launch_type") == "retrobat"
            was_disc = editing and (self.catalog.get(original_id) or {}).get("launch_type") == "disc"
        if was_disc and launch in ("direct", "disc") and not (form.get("exe_path") or "").strip():
            launch = "disc"  # jogo de disco físico: editar nome/capa/ficha não muda como ele roda
        elif launch == "disc":
            return False, "Jogos de disco entram sozinhos quando o disco é colocado no leitor.", None
        if was_rb and launch == "direct" and not (form.get("exe_path") or "").strip():
            launch = "retrobat"  # o formulário do desktop não conhece o tipo RetroBat: mantém como estava
        if launch == "retrobat" and not was_rb:
            return False, "Use “Adicionar ROM (RetroBat)” para jogos do RetroBat.", None
        uri = (form.get("launch_uri") or "").strip()
        install = (form.get("install_path") or "").strip()
        exe = (form.get("exe_path") or "").strip()
        rom = (form.get("rom_path") or "").strip()
        emu_id = form.get("emulator_id") or None
        with self.lock:
            if editing and original_id not in self.catalog:
                return False, "Jogo não encontrado.", None
            if name in self.catalog and name != original_id:
                return False, "Já existe um jogo com esse nome.", None
            if launch == "uri":
                if not uri:
                    return False, "Preencha o endereço de inicialização do jogo.", None
            elif launch == "emulator":
                if not self.emulators:
                    return False, "Cadastre um emulador antes de usar essa opção.", None
                if emu_id not in self.emulators:
                    return False, "Selecione um emulador.", None
                if not editing and not (rom and os.path.exists(rom)):
                    return False, "Selecione uma ROM/ISO válida.", None
            elif launch != "disc" and not editing and not (exe and os.path.exists(exe)):
                return False, "Selecione um executável válido.", None

            game = dict(self.catalog[original_id]) if editing else {}
            safe = safe_name(name)
            game.update(name=name, description=(form.get("description") or "").strip(),
                        source="disc" if launch == "disc" else "sd" if form.get("source") == "sd" else "local",
                        cover=self._adopt_art(form.get("cover"), safe, "cover"),
                        icon=self._adopt_art(form.get("icon"), safe, "icon"))
            for k in METADATA_KEYS:
                game[k] = (form.get(k) or "").strip()
            raw = str(form.get("rating") or "").strip().replace(",", ".")
            try:
                game["rating"] = round(float(raw), 1) if raw else ""
                if raw and not 0 <= game["rating"] <= 10:
                    raise ValueError
            except ValueError:
                return False, "A nota deve ser um número de 0 a 10.", None
            if launch == "disc":
                pass
            elif launch == "retrobat":
                if rom:
                    game["rom_path"] = rom
            elif launch == "uri":
                game.update(launch_type="uri", launch_uri=uri, install_path=install)
                for k in ("emulator_id", "rom_path"):
                    game.pop(k, None)
                if exe:
                    game["exe_path"] = exe
            elif launch == "emulator":
                game.update(launch_type="emulator", emulator_id=emu_id, rom_path=rom)
                game.pop("exe_path", None)
            else:
                game.update(launch_type="direct", exe_path=exe)
                game.pop("emulator_id", None)
                game.pop("rom_path", None)
            if game["source"] == "sd":
                target = {"direct": exe, "emulator": rom, "uri": install, "retrobat": rom or game.get("rom_path", "")}[launch]
                drive = ntpath.splitdrive(target)[0]
                if drive:
                    game["drive"] = drive + "\\"
                identity = sd_identity(target, self.mounted())
                if identity:  # cartão inserido agora: grava quem ele é (senão mantém o que já tinha)
                    game.update(identity)
            elif game["source"] != "disc":
                for k in ("drive", "sd_serial", "sd_name"):
                    game.pop(k, None)
            if editing and name != original_id:
                del self.catalog[original_id]
            self.catalog[name] = game
            save_json("catalog.json", self.catalog)
        return True, "", name

    def update_game(self, game_id, changes, remove=()):
        """Aplica um dicionário de mudanças a um jogo (usado pelo scraper)."""
        with self.lock:
            g = self.catalog.get(game_id)
            if g is None:
                return False
            g.update(changes)
            for k in remove:
                g.pop(k, None)
            save_json("catalog.json", self.catalog)
        return True

    def purge_systems(self, bad_systems):
        """Remove do catálogo itens do RetroBat que não são jogos (ex.: o menu de emuladores importado
        por engano na versão 1.1.0). Apaga só as artes copiadas para library/<nome>/. -> quantidade removida."""
        removed = []
        with self.lock:
            for k, g in list(self.catalog.items()):
                if g.get("launch_type") == "retrobat" and g.get("rb_system") in bad_systems:
                    removed.append(k)
                    for role in ("cover", "hero", "icon"):
                        p = resolve(g.get(role))
                        try:
                            if p and p.is_file() and LIBRARY.resolve() in p.resolve().parents:
                                folder = p.parent
                                p.unlink()
                                if folder.is_dir() and not any(folder.iterdir()):
                                    folder.rmdir()
                        except OSError:
                            pass
                    del self.catalog[k]
            if removed:
                save_json("catalog.json", self.catalog)
        return len(removed)

    def purge_duplicates(self):
        """Remove jogos do RetroBat repetidos (mesma ROM importada duas vezes; aconteceu na 1.2.0).
        Fica o primeiro; dos repetidos saem a entrada e as artes copiadas. -> quantidade removida."""
        seen, removed = set(), []
        with self.lock:
            for k, g in list(self.catalog.items()):
                if g.get("launch_type") != "retrobat" or not g.get("rom_path"):
                    continue
                key = (g.get("rb_system"), os.path.normcase(os.path.normpath(g["rom_path"])))
                if key in seen:
                    removed.append(k)
                    for role in ("cover", "hero", "icon"):
                        p = resolve(g.get(role))
                        try:
                            if p and p.is_file() and LIBRARY.resolve() in p.resolve().parents:
                                folder = p.parent
                                p.unlink()
                                if folder.is_dir() and not any(folder.iterdir()):
                                    folder.rmdir()
                        except OSError:
                            pass
                    del self.catalog[k]
                else:
                    seen.add(key)
            if removed:
                save_json("catalog.json", self.catalog)
        return len(removed)

    # ---------- ajustes rápidos (janela do jogo no modo console) ----------
    def set_art(self, game_id, role, src):
        """Copia uma imagem do PC para library/<jogo>/<role>.<ext> e usa como capa, banner ou ícone."""
        if role not in ("cover", "hero", "icon"):
            return False, "Tipo de imagem inválido."
        src = Path(src)
        if not src.is_file():
            return False, "Arquivo não encontrado."
        try:
            with Image.open(src) as im:
                im.verify()
        except Exception:
            return False, "Não consegui abrir essa imagem."
        with self.lock:
            g = self.catalog.get(game_id)
            if g is None:
                return False, "Jogo não encontrado."
            safe = safe_name(g.get("name", game_id))
        folder = LIBRARY / safe
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for old in folder.glob(f"{role}.*"):  # não deixa 'hero.jpg' antigo competindo com 'hero.png' novo
                old.unlink()
            dest = folder / f"{role}{src.suffix.lower()}"
            shutil.copyfile(src, dest)  # copyfile: data de modificação = agora (renova o cache do fundo)
        except OSError as e:
            return False, f"Não consegui copiar a imagem: {e}"
        self.update_game(game_id, {role: f"library/{safe}/{dest.name}"})
        return True, {"cover": "Capa", "hero": "Banner", "icon": "Ícone"}[role] + " atualizado."

    def set_target(self, game_id, path):
        """Troca o executável (ou a ROM, em jogos de emulador)."""
        if not path or not os.path.exists(path):
            return False, "Arquivo não encontrado."
        with self.lock:
            g = self.catalog.get(game_id)
            if g is None:
                return False, "Jogo não encontrado."
            emulator = g.get("launch_type") in ("emulator", "retrobat")
        if emulator:
            self.update_game(game_id, {"rom_path": path})
            return True, "ROM atualizada."
        # jogo de loja (steam://...) passa a abrir direto pelo .exe escolhido
        self.update_game(game_id, {"launch_type": "direct", "exe_path": path}, remove=("launch_uri",))
        return True, "Executável atualizado."

    @staticmethod
    def suggest_name(exe_path):
        """Nome provável a partir do .exe: usa a pasta quando o arquivo tem nome genérico."""
        p = Path(exe_path)
        generic = {"game", "launcher", "start", "play", "main", "app", "setup", "run", "client", "win64", "shipping"}
        stem = p.stem
        while True:  # tira sufixos técnicos: Game-Win64-Shipping -> Game
            cut = re.sub(r"[-_. ](win(32|64)|x64|x86|shipping|dx1[12]|vulkan|launcher)$", "", stem, flags=re.I)
            if cut == stem:
                break
            stem = cut
        name = stem
        skip = {"bin", "binaries", "win64", "win32", "x64", "x86", "game", "games", "common", "program files",
                "program files (x86)", "steamapps", "epic games", "gog games"}
        if stem.lower() in generic or len(stem) <= 2:
            for part in reversed(p.parts[:-1]):
                if part.lower() not in skip and not re.match(r"^[a-z]:[\\/]?$", part, re.I) and part not in ("/", "\\"):
                    name = part
                    break
        name = re.sub(r"[_.]+", " ", name)
        name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).strip()
        if name and name == name.lower():
            name = name.title()
        return name[:1].upper() + name[1:] if name else p.stem

    def art_path(self, game_id, kind):
        """Caminho absoluto da arte (cover/hero/icon) do jogo, ou None."""
        p = resolve((self.catalog.get(game_id) or {}).get(kind))
        return p if p and p.is_file() else None

    def get_art(self, game_id, kind):
        """Imagem grande sob demanda (fundo do detalhe) -> data URI | None."""
        g = self.catalog.get(game_id) or {}
        return load_art(resolve(g.get(kind)), kind)[0]

    def delete_game(self, game_id):
        with self.lock:
            existed = self.catalog.pop(game_id, None) is not None
            if existed:
                save_json("catalog.json", self.catalog)
        return existed

    # ---------- emuladores ----------
    def _games_using(self, emulator_id):
        with self.lock:
            return sum(1 for g in self.catalog.values() if g.get("emulator_id") == emulator_id)

    def list_emulators(self):
        with self.lock:
            items = list(self.emulators.items())
        return [{"id": i, "name": e.get("name", i), "executable": e.get("executable", ""), "core": e.get("core", ""),
                 "args_template": e.get("args_template", '"{rom}"'), "rom_extensions": e.get("rom_extensions", ""),
                 "found": os.path.exists(e.get("executable", "")), "games": self._games_using(i)}
                for i, e in sorted(items, key=lambda kv: kv[1].get("name", "").casefold())]

    def get_emulator_form(self, emulator_id=None):
        e = self.emulators.get(emulator_id) or {}
        return {"games": self._games_using(emulator_id) if emulator_id else 0,
                "form": {"name": e.get("name", ""), "executable": e.get("executable", ""), "core": e.get("core", ""),
                         "args_template": e.get("args_template", '"{rom}"'), "rom_extensions": e.get("rom_extensions", "")}}

    def save_emulator(self, form, original_id=None):
        name = (form.get("name") or "").strip()
        exe = (form.get("executable") or "").strip()
        if not name or not exe:
            return False, "Preencha ao menos o nome e o executável.", None
        template = (form.get("args_template") or "").strip() or '"{rom}"'
        try:
            template.format(rom="x", core="y")
        except (KeyError, IndexError, ValueError):
            return False, "Template de argumentos inválido (use só {rom} e {core}).", None
        if "{rom}" not in template:
            return False, "O template de argumentos precisa conter {rom}.", None
        with self.lock:
            if original_id and original_id not in self.emulators:
                return False, "Emulador não encontrado.", None
            if any(i != original_id and e.get("name", "").casefold() == name.casefold() for i, e in self.emulators.items()):
                return False, "Já existe um emulador com esse nome.", None
            data = dict(self.emulators.get(original_id) or {})
            data.update(name=name, executable=exe, core=(form.get("core") or "").strip(), args_template=template,
                        rom_extensions=", ".join(parse_exts(form.get("rom_extensions"))))
            eid = original_id
            if not eid:
                base = "".join(c for c in name.lower() if c.isalnum() or c == " ").strip().replace(" ", "_") or "emulador"
                eid, n = base, 2
                while eid in self.emulators:
                    eid, n = f"{base}_{n}", n + 1
            self.emulators[eid] = data
            save_json("emulators.json", self.emulators)
        return True, "", eid

    def delete_emulator(self, emulator_id):
        with self.lock:
            existed = self.emulators.pop(emulator_id, None) is not None
            if existed:
                save_json("emulators.json", self.emulators)
        return existed

    # ---------- ROMs ----------
    def rom_exts(self, emulator_id):
        return parse_exts((self.emulators.get(emulator_id) or {}).get("rom_extensions")) or list(DEFAULT_ROM_EXTS)

    def rom_file_types(self, emulator_id):
        return ("ROMs (" + ";".join("*." + e for e in self.rom_exts(emulator_id)) + ")", "Todos os arquivos (*.*)")

    def rom_candidates(self, emulator_id, paths):
        """Arquivos escolhidos -> [{path, name, dup}] (dup = essa ROM já está cadastrada nesse emulador)."""
        with self.lock:
            have = {_norm(g["rom_path"]) for g in self.catalog.values()
                    if g.get("emulator_id") == emulator_id and g.get("rom_path")}
        return [{"path": p, "name": clean_rom_name(os.path.splitext(os.path.basename(p))[0]),
                 "dup": _norm(p) in have} for p in paths]

    def scan_folder(self, emulator_id, folder, max_depth=4, limit=500):
        exts, found = set(self.rom_exts(emulator_id)), []
        base = folder.rstrip("\\/").count(os.sep)
        for root, dirs, files in os.walk(folder):
            if root.count(os.sep) - base >= max_depth:
                dirs[:] = []
            cues = {os.path.splitext(f)[0] for f in files if f.lower().endswith(".cue")}
            for f in sorted(files, key=str.casefold):
                stem, ext = os.path.splitext(f)
                ext = ext.lstrip(".").lower()
                if ext in exts and not (ext == "bin" and stem in cues):  # .bin de um .cue não entra duas vezes
                    found.append(os.path.join(root, f))
        return self.rom_candidates(emulator_id, found[:limit])

    def import_roms(self, emulator_id, items):
        """Cria um jogo por ROM. Retorna (adicionados, ignorados)."""
        if emulator_id not in self.emulators:
            return 0, len(items)
        mounted, added, skipped = self.mounted(), 0, 0
        with self.lock:
            for it in items:
                path = (it.get("path") or "").strip()
                if not path or not os.path.isfile(path):
                    skipped += 1
                    continue
                base = (it.get("name") or "").strip() or clean_rom_name(os.path.splitext(os.path.basename(path))[0])
                name, n = base, 2
                while name in self.catalog:
                    name, n = f"{base} ({n})", n + 1
                entry = {"name": name, "description": "", "cover": None, "icon": None, "launch_type": "emulator",
                         "emulator_id": emulator_id, "rom_path": path, "source": "local"}
                entry.update({k: "" for k in METADATA_KEYS})
                identity = sd_identity(path, mounted)
                if identity:  # ROM dentro de um cartão SD
                    entry.update(source="sd", drive=ntpath.splitdrive(path)[0] + "\\", **identity)
                self.catalog[name] = entry
                added += 1
            if added:
                save_json("catalog.json", self.catalog)
        return added, skipped

    # ---------- dados pra interface ----------
    def _sys_names(self, items):
        """rb_system -> nome do sistema, tirado das ROMs do RetroBat (para o disco cair no mesmo grupo e nome)."""
        out = {}
        for _, g in items:
            if g.get("launch_type") == "retrobat" and g.get("rb_system") and g.get("rb_system_name"):
                out.setdefault(g["rb_system"], g["rb_system_name"])
        return out

    def _sys(self, g, names):
        """(sistema, nome) do jogo na aba Emuladores: ROMs do RetroBat e discos físicos (ps2, psx…)."""
        if g.get("launch_type") == "retrobat":
            return g.get("rb_system", ""), g.get("rb_system_name", "")
        if g.get("launch_type") == "disc" and g.get("disc_system"):
            from .discs import RB_NAMES, SYSTEMS
            k = g["disc_system"]
            return k, names.get(k) or RB_NAMES.get(k) or SYSTEMS.get(k, k)
        return "", ""

    def to_ui(self, key, g, mounted=None, names=None):
        mounted = self.mounted() if mounted is None else mounted
        if names is None:
            with self.lock:
                names = self._sys_names(list(self.catalog.items()))
        sys_k, sys_n = self._sys(g, names)
        cover, hue = load_art(resolve(g.get("cover")), "cover")
        icon, _ = load_art(resolve(g.get("icon")), "icon")
        h1 = hue if hue is not None else hash_hue(g.get("name", key))
        emu = self.emulators.get(g.get("emulator_id", ""), {}) if g.get("launch_type") == "emulator" else {}
        src = self.source(g)
        cart = self._match_cart(g, mounted) if src == "sd" else None
        disc = getattr(self, "discs", None).find(g) if g.get("launch_type") == "disc" and getattr(self, "discs", None) else None
        dash = lambda k: g.get(k) or "—"
        return {
            "id": key, "n": g.get("name", key), "d": g.get("description", ""),
            "s": src, "active": self._paths(g, mounted) is not None,
            "drive": disc or (cart["drive"] if cart else g.get("drive", "")),
            "platform": g.get("platform") or emu.get("name") or g.get("rb_system_name") or "PC",
            "genre": g.get("genre", ""), "dev": dash("developer"), "year": dash("release_date"),
            "size": dash("size"), "time": dash("playtime"),
            "h": [h1, (h1 + 40) % 360], "img": cover, "ic": icon,
            "store": g.get("store", ""), "lt": g.get("launch_type") or "direct",
            "sys": sys_k, "sysn": sys_n,
            "rating": g["rating"] if isinstance(g.get("rating"), (int, float)) else None,
            "has_hero": bool((p := resolve(g.get("hero"))) and p.is_file()),
        }

    def list_ui(self):
        with self.lock:
            items = list(self.catalog.items())
        mounted, names = self.mounted(), self._sys_names(items)
        return [self.to_ui(k, g, mounted, names) for k, g in sorted(items, key=lambda kv: kv[0].casefold())]
