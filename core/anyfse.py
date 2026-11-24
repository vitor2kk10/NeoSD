"""Integração com o AnyFSE (github.com/ashpynov/AnyFSE, licença MIT).

O AnyFSE é o 'app inicial' que o modo Xbox do Windows aceita, e ele abre o launcher escolhido pelo usuário.
O que o NeoSD faz aqui:
  - detecta se o AnyFSE está instalado (pasta em Arquivos de Programas ou registro de desinstalação);
  - se o instalador oficial veio junto (third_party/AnyFSE/AnyFSE.Installer.exe, baixado pelo build.bat),
    instala em modo silencioso (/s, documentado no README do AnyFSE; pede administrador);
  - abre a configuração do AnyFSE para o usuário escolher 'executável personalizado'.

O formato da configuração interna do AnyFSE não é documentado, então o NeoSD não grava nada nela às cegas:
ele mostra o Programa e os Argumentos para colar. A ferramenta tools/anyfse-diagnostico.ps1 levanta onde o
AnyFSE guarda a escolha, para automatizar isso numa próxima versão sem risco de corromper a configuração.
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None

log = logging.getLogger("neosd.anyfse")
UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\AnyFSE"
INSTALLER = "AnyFSE.Installer.exe"


def app_dir():
    """Pasta do programa (ao lado do .exe quando instalado; raiz do projeto rodando pelo Python)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


class AnyFSEService:
    def __init__(self, system, env=None, runner=subprocess.run, popen=subprocess.Popen):
        self.system, self.runner, self.popen = system, runner, popen
        self.env = os.environ if env is None else env

    # ---------- onde está ----------
    def install_dir(self):
        if winreg is not None:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, UNINSTALL) as k:
                        loc = winreg.QueryValueEx(k, "InstallLocation")[0]
                        if loc and Path(loc).is_dir():
                            return Path(loc)
                except OSError:
                    pass
        for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            base = self.env.get(var)
            if base and (Path(base) / "AnyFSE").is_dir():
                return Path(base) / "AnyFSE"
        return None

    def bundled_installer(self):
        p = app_dir() / "third_party" / "AnyFSE" / INSTALLER
        return p if p.is_file() else None

    def _settings_exe(self, folder):
        """O executável de configuração (projeto AnyFSE.Settings); se o nome mudar, cai no principal."""
        for name in ("AnyFSE.Settings.exe", "AnyFSE.exe"):
            if (folder / name).is_file():
                return folder / name
        exes = sorted(folder.glob("*.exe"))
        return exes[0] if exes else None

    def status(self):
        folder = self.install_dir()
        exe, args = self.system.launch_parts(True)
        return {"installed": bool(folder), "folder": str(folder or ""),
                "bundled": bool(self.bundled_installer()),
                "program": exe, "arguments": " ".join(self.system._q(a) for a in args)}

    # ---------- ações ----------
    def install(self):
        """Roda o instalador oficial em modo silencioso, com o pedido de administrador do Windows."""
        inst = self.bundled_installer()
        if not inst:
            return False, "O instalador do AnyFSE não veio junto com esta cópia do NeoSD."
        from .elevate import run_as_admin
        status, code = run_as_admin(inst, "/s", show=1, timeout=900, cwd=inst.parent)
        if status == "cancelled":
            return False, "Instalação cancelada no pedido de administrador."
        if status != "ok":
            log.warning("instalador do AnyFSE: %s", status)
            return False, "Não consegui iniciar o instalador do AnyFSE."
        r = type("R", (), {"returncode": code, "stderr": ""})()
        if r.returncode == 0 and self.install_dir():
            return True, "AnyFSE instalado. Agora escolha o NeoSD na configuração dele."
        log.warning("instalador do AnyFSE saiu com %s: %s", r.returncode, (r.stderr or "")[-400:])
        if "canceled" in (r.stderr or "").lower() or "cancelad" in (r.stderr or "").lower():
            return False, "Instalação cancelada no pedido de administrador."
        return False, "O instalador do AnyFSE não terminou com sucesso."

    def open_settings(self):
        folder = self.install_dir()
        if not folder:
            return False, "O AnyFSE não está instalado."
        exe = self._settings_exe(folder)
        try:
            if exe:
                self.popen([str(exe)], cwd=str(folder))
            else:
                os.startfile(str(folder))  # type: ignore[attr-defined]
        except Exception as e:
            log.exception("abrir configuração do AnyFSE")
            return False, f"Não consegui abrir o AnyFSE: {e}"
        return True, "Configuração do AnyFSE aberta. Escolha “executável personalizado” e cole o Programa e os Argumentos."
