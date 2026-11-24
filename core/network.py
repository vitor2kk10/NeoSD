"""Compartilhar a pasta de ROMs do RetroBat na rede de casa (como o \\\\BATOCERA\\share do Batocera).

Ativar (uma vez, com o pedido de administrador do Windows):
  - cria o compartilhamento "ROMs" -> <RetroBat>\\roms, com acesso só para a conta do usuário (nunca "Todos");
  - cria a regra de firewall "NeoSD - ROMs na rede": entrada TCP 445 (compartilhamento de arquivos), só em
    rede Privada e só para endereços da rede local; nada muda em redes públicas (Wi-Fi de café etc.);
  - opcionalmente, muda a rede atual de Pública para Privada.
Desativar remove o compartilhamento e a regra de firewall do NeoSD.

No outro PC: Explorador -> \\\\NOME-DO-PC\\ROMs, entrando com o usuário e a senha da conta do PC console.
"""
import json
import logging
import os
import re
import socket
import subprocess
from pathlib import Path

from .elevate import run_ps_as_admin

log = logging.getLogger("neosd.network")
SHARE = "ROMs"
FW_RULE = "NeoSD-ROMs-SMB"
NOWIN = 0x08000000  # CREATE_NO_WINDOW

ENABLE_PS1 = r"""
$Path = $P.Path; $User = $P.User; $Name = $P.Name; $Private = [int]$P.Private
$r = @{ ok = $false; msg = "" }
try {
  $old = Get-SmbShare -Name $Name -ErrorAction SilentlyContinue
  if ($old -and $old.Path -ne $Path) { throw "SHARE_TAKEN" }
  if (-not $old) { New-SmbShare -Name $Name -Path $Path -FullAccess $User -Description "NeoSD - ROMs do RetroBat" | Out-Null }
  else { Grant-SmbShareAccess -Name $Name -AccountName $User -AccessRight Full -Force | Out-Null }
  if ($Private -eq 1) {
    Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -eq 'Public' } |
      ForEach-Object { Set-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -NetworkCategory Private }
  }
  if (-not (Get-NetFirewallRule -Name "NeoSD-ROMs-SMB" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name "NeoSD-ROMs-SMB" -DisplayName "NeoSD - ROMs na rede (SMB)" -Direction Inbound -Protocol TCP `
      -LocalPort 445 -Profile Private -RemoteAddress LocalSubnet -Action Allow | Out-Null
  }
  $r.ok = $true
} catch { $r.msg = $_.Exception.Message }
$r | ConvertTo-Json -Compress | Set-Content -Path $Out -Encoding UTF8
"""

DISABLE_PS1 = r"""
$Name = $P.Name
$r = @{ ok = $false; msg = "" }
try {
  Remove-SmbShare -Name $Name -Force -ErrorAction SilentlyContinue
  Remove-NetFirewallRule -Name "NeoSD-ROMs-SMB" -ErrorAction SilentlyContinue
  $r.ok = $true
} catch { $r.msg = $_.Exception.Message }
$r | ConvertTo-Json -Compress | Set-Content -Path $Out -Encoding UTF8
"""


def _ps(cmd, runner=subprocess.run, timeout=20):
    try:
        r = runner(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                   capture_output=True, text=True, timeout=timeout, creationflags=NOWIN if os.name == "nt" else 0)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:  # sem PowerShell, tempo esgotado…
        return 1, "", str(e)


class NetworkService:
    def __init__(self, settings, retrobat, env=None, runner=subprocess.run):
        self.settings, self.rb, self.runner = settings, retrobat, runner
        self.env = os.environ if env is None else env

    # ---------- informações ----------
    def computer(self):
        return self.env.get("COMPUTERNAME") or socket.gethostname()

    def user(self):
        dom, usr = self.env.get("USERDOMAIN", ""), self.env.get("USERNAME", "")
        return f"{dom}\\{usr}" if dom and usr else usr

    @staticmethod
    def addresses():
        ips = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith(("127.", "169.254.")):
                    ips.add(ip)
        except OSError:
            pass
        return sorted(ips)

    def roms_dir(self):
        r = self.rb.root() if self.rb else None
        return r / "roms" if r else None

    def share_path(self):
        """Caminho do compartilhamento ROMs, se existir (lido com 'net share', que não pede administrador)."""
        try:
            r = self.runner(["net", "share", SHARE], capture_output=True, text=True, timeout=10,
                            creationflags=NOWIN if os.name == "nt" else 0)
        except Exception:
            return ""
        if r.returncode != 0:
            return ""
        for line in (r.stdout or "").splitlines():
            m = re.match(r"^\s*(Path|Caminho|Chemin|Ruta|Pfad)\s+(.+)$", line, re.I)
            if m:
                return m.group(2).strip()
        return ""

    def profiles(self):
        code, out, _ = _ps("Get-NetConnectionProfile | Select-Object Name,NetworkCategory | ConvertTo-Json -Compress",
                           self.runner)
        if code or not out:
            return []
        try:
            data = json.loads(out)
        except ValueError:
            return []
        data = data if isinstance(data, list) else [data]
        cat = {0: "Public", 1: "Private", 2: "DomainAuthenticated"}
        return [{"name": d.get("Name", ""), "category": cat.get(d.get("NetworkCategory"), str(d.get("NetworkCategory")))}
                for d in data]

    def status(self):
        roms = self.roms_dir()
        path = self.share_path() if os.name == "nt" else ""
        shared = bool(path and roms and os.path.normcase(os.path.normpath(path)) == os.path.normcase(os.path.normpath(str(roms))))
        profs = self.profiles() if os.name == "nt" else []
        pc = self.computer()
        return {"retrobat": bool(roms), "roms": str(roms or ""), "computer": pc, "user": self.user(),
                "ips": self.addresses(), "shared": shared, "share_other": bool(path) and not shared,
                "unc": f"\\\\{pc}\\{SHARE}", "public": any(p["category"] == "Public" for p in profs),
                "profiles": profs, "auto_import": bool(self.settings.get("net_auto_import", True))}

    # ---------- ações (elevadas) ----------
    def _elevated(self, script, args):
        """Roda o script como administrador (pedido do UAC na frente do NeoSD) e devolve o JSON que ele grava."""
        data, err = run_ps_as_admin(script, args)
        return data if data is not None else {"ok": False, "msg": err}

    def enable(self, make_private=False):
        roms = self.roms_dir()
        if os.name != "nt":
            return False, "Compartilhar pela rede só funciona no Windows."
        if not roms or not roms.is_dir():
            return False, "RetroBat não encontrado. Indique a pasta em Configurações → Emulação."
        res = self._elevated(ENABLE_PS1, {"Path": str(roms), "User": self.user(), "Name": SHARE,
                                          "Private": 1 if make_private else 0})
        if not res.get("ok"):
            msg = res.get("msg", "")
            if msg == "SHARE_TAKEN":
                return False, f"Já existe outro compartilhamento chamado “{SHARE}” neste PC. Remova-o ou renomeie e tente de novo."
            log.warning("falha ao compartilhar: %s", msg)
            return False, msg or "Não consegui criar o compartilhamento."
        return True, f"Pronto! No outro PC, abra \\\\{self.computer()}\\{SHARE}"

    def disable(self):
        if os.name != "nt":
            return False, "Só funciona no Windows."
        res = self._elevated(DISABLE_PS1, {"Name": SHARE})
        if not res.get("ok"):
            return False, res.get("msg") or "Não consegui remover o compartilhamento."
        return True, "Compartilhamento desligado."

    def open_folder(self):
        roms = self.roms_dir()
        if not roms:
            return False, "RetroBat não encontrado."
        try:
            os.startfile(str(roms))  # type: ignore[attr-defined]
            return True, ""
        except Exception as e:
            return False, str(e)
