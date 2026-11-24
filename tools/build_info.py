"""Gera core/_build_info.py (versão, número do build, canal e repositório do GitHub) para o atualizador.

Valores (o que existir primeiro):
  - NEOSD_BUILD, NEOSD_CHANNEL ("estavel" | "teste"), NEOSD_REPO ("usuario/NeoSD") — passados pelo GitHub Actions;
  - build local (build.bat): build 0, canal "local", repositório lido do .git/config (remote "origin" do GitHub).
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def repo_from_git():
    cfg = ROOT / ".git" / "config"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', text, re.S)
    if not m:
        return ""
    m2 = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", m.group(1))
    return f"{m2.group(1)}/{m2.group(2)}" if m2 else ""


def main():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    build = int(os.environ.get("NEOSD_BUILD") or 0)
    channel = os.environ.get("NEOSD_CHANNEL") or "local"
    repo = os.environ.get("NEOSD_REPO") or repo_from_git()
    (ROOT / "core" / "_build_info.py").write_text(
        "# Gerado pelo build (tools/build_info.py). Não versionar.\n"
        f"VERSION = {version!r}\nBUILD = {build}\nCHANNEL = {channel!r}\nREPO = {repo!r}\n", encoding="utf-8")
    print(f"Build: NeoSD {version} · build {build} · canal {channel} · repositório {repo or '(nenhum)'}")


if __name__ == "__main__":
    main()
