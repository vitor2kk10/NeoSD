"""Integração com o Windows: iniciar com o sistema, atalho do modo console e o passo a passo pra usar o
NeoSD como app inicial do modo Xbox (Full Screen Experience).

Escolha de projeto: o Windows só lista como 'app inicial' pacotes MSIX assinados com uma capability especial.
Em vez de instalar certificados ou mexer no shell, o NeoSD vira um 'executável personalizado' do AnyFSE,
que já faz essa ponte. Aqui ficam só os pedaços seguros e reversíveis."""
import os
import subprocess
import sys
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None

RUN_KEY, RUN_NAME = r"Software\Microsoft\Windows\CurrentVersion\Run", "NeoSD"
LEGACY_RUN_NAMES = ("RetroSD",)  # nome antigo do projeto


class SystemService:
    def __init__(self, settings, base, frozen=None, runner=subprocess.run):
        self.settings, self.base, self.runner = settings, Path(base), runner
        self.frozen = getattr(sys, "frozen", False) if frozen is None else frozen

    # ---------- como iniciar ----------
    def launch_parts(self, console=True):
        """(programa, [argumentos]) que abre o NeoSD (em modo console, por padrão)."""
        if self.frozen:
            exe, args = sys.executable, []
        else:
            exe = sys.executable
            pyw = exe[:-len("python.exe")] + "pythonw.exe" if exe.lower().endswith("python.exe") else exe
            exe = pyw if os.path.exists(pyw) else exe  # pythonw = sem janela de terminal
            args = [str(self.base / "main.py")]
        return exe, args + (["--console"] if console else [])

    @staticmethod
    def _q(s):
        return f'"{s}"' if " " in s or not s else s

    def command_line(self, console=True):
        exe, args = self.launch_parts(console)
        return " ".join(self._q(x) for x in [exe] + args)

    # ---------- iniciar com o Windows (chave Run do usuário; reversível) ----------
    def autostart_enabled(self):
        if winreg is None:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, RUN_NAME)
                return True
        except OSError:
            return False

    def set_autostart(self, on):
        if winreg is None:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                for legacy in LEGACY_RUN_NAMES:  # apaga a entrada do nome antigo, se houver
                    try:
                        winreg.DeleteValue(k, legacy)
                    except OSError:
                        pass
                if on:
                    winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, self.command_line(console=bool(self.settings.get("start_console"))))
                else:
                    try:
                        winreg.DeleteValue(k, RUN_NAME)
                    except FileNotFoundError:
                        pass
            return True
        except OSError:
            return False

    # ---------- atalho ----------
    def create_shortcuts(self):
        exe, args = self.launch_parts(True)
        esc = lambda s: s.replace("'", "''")
        ps = ("$w=New-Object -ComObject WScript.Shell;"
              "foreach($d in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))){"
              "$s=$w.CreateShortcut((Join-Path $d 'NeoSD Console.lnk'));"
              f"$s.TargetPath='{esc(exe)}';$s.Arguments='{esc(' '.join(self._q(a) for a in args))}';"
              f"$s.WorkingDirectory='{esc(str(self.base))}';$s.Save()}}")
        try:
            r = self.runner(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                            capture_output=True, text=True, timeout=30)
        except Exception as e:
            return False, f"Não consegui criar o atalho: {e}"
        return (True, "Atalho “NeoSD Console” criado na Área de Trabalho e no Menu Iniciar.") if r.returncode == 0 \
            else (False, "O PowerShell não conseguiu criar o atalho.")

    # ---------- estado pra interface ----------
    def info(self):
        exe, args = self.launch_parts(True)
        return {"start_console": bool(self.settings.get("start_console", False)), "autostart": self.autostart_enabled(),
                "exe": exe, "args": " ".join(self._q(a) for a in args), "command": self.command_line(True),
                "steps": [
                    "Ative o Modo Xbox (tela cheia) no seu PC com a ferramenta XboxFullScreenExperienceTool (github.com/8bit2qubit/XboxFullScreenExperienceTool). Em handhelds que já têm o modo nativo, como o ROG Xbox Ally, isso já vem pronto.",
                    "Instale o AnyFSE: pelo botão “Instalar o AnyFSE” do NeoSD (quando ele vem junto) ou em github.com/ashpynov/AnyFSE. Ele se registra como “app inicial” do modo Xbox e abre o launcher que você escolher.",
                    "Abra a configuração do AnyFSE, escolha um executável personalizado e informe o Programa e os Argumentos mostrados abaixo.",
                    "Nas Configurações do Windows → Jogos → Experiência em tela cheia, escolha AnyFSE como app inicial e ative “Entrar na experiência em tela cheia ao iniciar”.",
                    "Reinicie. Para voltar ao app Xbox original, escolha “Xbox” como app inicial de novo; nada foi desinstalado nem alterado no Windows pelo NeoSD."]}

    def set_option(self, key, value):
        if key == "start_console":
            self.settings.set("start_console", bool(value))
            if self.autostart_enabled():  # mantém o comando do 'iniciar com o Windows' coerente
                self.set_autostart(True)
        elif key == "autostart":
            if not self.set_autostart(bool(value)):
                return False, "Não consegui alterar a inicialização com o Windows."
        else:
            return False, "Opção desconhecida."
        return True, ""
