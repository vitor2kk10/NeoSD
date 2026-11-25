"""Jogos com vários discos (PS1, PS2, GameCube, Saturn, Dreamcast, Sega CD…).

Dois casos:
  - Discos físicos: cada disco tem um código diferente (Final Fantasy VII = SCUS-94163/94164/94165). O NeoSD junta os
    discos num jogo só quando o nome indica o número do disco ("… (Disc 2)", "CD2", "DISC_2") ou quando o
    RetroAchievements diz que é o mesmo jogo; senão o usuário junta na janela do disco novo.
  - ROMs: "Jogo (Disc 1).cue" + "Jogo (Disc 2).cue" viram um "Jogo.m3u" (lista de discos) na mesma pasta. É o padrão
    do RetroBat/EmulationStation: o jogo aparece uma vez e o emulador troca de disco sozinho (DuckStation, PCSX2,
    Dolphin, Flycast, RetroArch…). Só nos sistemas que aceitam .m3u no es_systems.cfg.
"""
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("neosd.multidisc")

# "(Disc 1)", "(Disc 1 of 3)", "[Disc2]", "- CD 2", "_DISC_2", "Disco 2", "Disk B"
DISC_RE = re.compile(
    r"[\s._-]*[(\[]?\s*(?:(?:disc|disk|disco)\s*[._-]?\s*(?P<n>\d{1,2}|[a-d](?![a-z]))|cd\s*[._-]?\s*(?P<c>\d{1,2}))"
    r"(?![0-9])(?:\s*(?:of|de|/)\s*(?P<t>\d{1,2}))?\s*[)\]]?",
    re.I)


def split_disc(name):
    """'Final Fantasy VII (USA) (Disc 2)' -> ('Final Fantasy VII (USA)', 2) ; sem número -> (nome, 0)."""
    name = name or ""
    # o número do disco vem no fim do nome (depois dele só pode haver "(v1.1)", "[!]", "(USA)"…)
    hits = [m for m in DISC_RE.finditer(name) if re.fullmatch(r"(?:\s*[(\[][^)\]]*[)\]])*\s*", name[m.end():])]
    if not hits:
        return name.strip(), 0
    m = hits[-1]
    v = (m.group("n") or m.group("c") or "").lower()
    n = int(v) if v.isdigit() else ord(v) - 96
    if n <= 0:
        return name.strip(), 0
    base = (name[:m.start()] + " " + name[m.end():]).strip()
    base = re.sub(r"\(\s*\)|\[\s*\]", "", base)
    base = re.sub(r"\s{2,}", " ", base).strip(" ._-")
    return (base or name.strip()), n


def norm(name):
    """Forma para comparar nomes: sem região/parênteses, sem pontuação, minúsculas."""
    s = re.sub(r"[(\[].*?[)\]]", " ", name or "")
    return re.sub(r"[^0-9a-z]+", "", s.lower())


def read_m3u(path):
    """-> [caminhos absolutos dos discos listados]."""
    p = Path(path)
    out = []
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip().lstrip("﻿")
            if line and not line.startswith("#"):
                q = Path(line)
                out.append(str(q if q.is_absolute() else (p.parent / q)))
    except OSError:
        pass
    return out


def groups(paths):
    """Arquivos (não .m3u) da MESMA pasta e extensão cujo nome só muda no número do disco.
    -> [(pasta, nome_base, [(n, caminho), …])] com 2+ discos diferentes."""
    by = {}
    for p in paths:
        q = Path(p)
        if q.suffix.lower() == ".m3u" or not q.suffix:
            continue
        base, n = split_disc(q.stem)
        if n:
            by.setdefault((str(q.parent), norm(base), q.suffix.lower()), (base, []))[1].append((n, str(q)))
    out = []
    for (folder, _, _), (base, items) in by.items():
        items = sorted({n: p for n, p in items}.items())
        if len(items) >= 2:
            out.append((folder, base, items))
    return out


def write_m3u(folder, base, items):
    """Cria '<base>.m3u' com os discos em ordem (nomes relativos). Não sobrescreve um que já exista.
    -> caminho do .m3u, ou None se a pasta não deixar gravar."""
    safe = re.sub(r'[<>:"/\\|?*]', "", base).strip() or "Jogo"
    m3u = Path(folder) / f"{safe}.m3u"
    if m3u.exists():
        return str(m3u)
    try:
        m3u.write_text("\n".join(Path(p).name for _, p in items) + "\n", encoding="utf-8")
        log.info("lista de discos criada: %s", m3u)
        return str(m3u)
    except OSError as e:
        log.info("não consegui criar %s: %s", m3u, e)
        return None


def siblings(path, exts):
    """Para uma ROM escolhida à mão ('Jogo (Disc 1).cue'): os outros discos na mesma pasta. -> (base, items) ou None."""
    q = Path(path)
    try:
        names = [str(q.parent / e) for e in os.listdir(q.parent) if os.path.splitext(e)[1].lower() in exts]
    except OSError:
        return None
    for folder, base, items in groups(names):
        if any(os.path.normcase(p) == os.path.normcase(str(q)) for _, p in items):
            return base, items
    return None
