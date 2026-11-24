"""Executar algo como administrador mostrando o pedido do UAC NA FRENTE do NeoSD.

Antes usávamos 'powershell Start-Process -Verb RunAs' a partir de um processo sem janela: nesse caminho o
Windows pode deixar o pedido de administrador escondido atrás da janela (ou nem mostrar) e o NeoSD ficava sem
resposta. Aqui chamamos ShellExecuteExW("runas") direto, com a janela do NeoSD como dona do pedido, e
esperamos o processo terminar. A saída de scripts PowerShell (inclusive erros de sintaxe) vai para um log.
"""
import ctypes
import json
import logging
import os
import shutil
import tempfile
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger("neosd.elevate")
ERROR_CANCELLED = 1223
SEE_MASK_NOCLOSEPROCESS, SEE_MASK_NOASYNC = 0x40, 0x100


class _SEI(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


def run_as_admin(file, params="", show=0, timeout=600, cwd=None):
    """-> (status, exit_code). status: 'ok' | 'cancelled' | 'timeout' | 'error:<mensagem>'."""
    return shell_run(file, params, "runas", show, timeout, cwd)


def shell_run(file, params="", verb="open", show=1, timeout=600, cwd=None):
    """Abre um programa pelo Shell e espera ele terminar. Com verb='open', um instalador que exige
    administrador mostra o pedido do UAC sozinho (preso à janela do NeoSD). -> (status, exit_code)."""
    if os.name != "nt":
        return "error:só funciona no Windows", None
    shell32, kernel32, user32 = ctypes.windll.shell32, ctypes.windll.kernel32, ctypes.windll.user32
    sei = _SEI()
    sei.cbSize = ctypes.sizeof(_SEI)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    sei.hwnd = user32.GetForegroundWindow()  # o pedido do UAC aparece preso (e na frente) desta janela
    sei.lpVerb, sei.lpFile, sei.lpParameters = verb, str(file), params
    sei.lpDirectory = str(cwd) if cwd else None
    sei.nShow = show
    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = kernel32.GetLastError()
        if err == ERROR_CANCELLED:
            return "cancelled", None
        return f"error:{ctypes.FormatError(err).strip()} ({err})", None
    if not sei.hProcess:
        return "ok", 0
    try:
        if kernel32.WaitForSingleObject(sei.hProcess, int(timeout * 1000)) != 0:
            return "timeout", None
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        return "ok", code.value
    finally:
        kernel32.CloseHandle(sei.hProcess)


def run_ps_as_admin(script, params, timeout=180):
    """Roda um script PowerShell como administrador. O script recebe $P (parâmetros, lidos de params.json)
    e deve gravar o resultado em $Out (JSON). -> (dict | None, mensagem de erro)."""
    tmp = Path(tempfile.mkdtemp(prefix="neosd-admin-"))
    try:
        (tmp / "params.json").write_text(json.dumps(params), encoding="utf-8")
        head = ('$P = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot "params.json") | ConvertFrom-Json\n'
                '$Out = Join-Path $PSScriptRoot "resultado.json"\n')
        (tmp / "acao.ps1").write_text(head + script, encoding="utf-8-sig")
        ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        cmd = Path(os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe"))
        inner = f'"{ps}" -NoProfile -ExecutionPolicy Bypass -File "{tmp / "acao.ps1"}" > "{tmp / "saida.log"}" 2>&1'
        status, code = run_as_admin(cmd, f'/c "{inner}"', show=0, timeout=timeout, cwd=tmp)
        if status == "cancelled":
            return None, "Cancelado no pedido de administrador do Windows."
        if status == "timeout":
            return None, "O Windows demorou demais para responder."
        if status.startswith("error:"):
            return None, "Não consegui pedir permissão de administrador: " + status[6:]
        try:
            return json.loads((tmp / "resultado.json").read_text(encoding="utf-8-sig")), ""
        except (OSError, ValueError):
            try:
                out = (tmp / "saida.log").read_text(encoding="utf-8", errors="replace")[-2000:]
            except OSError:
                out = ""
            log.warning("script de administrador sem resultado (código %s). Saída:\n%s", code, out)
            last = next((l.strip() for l in reversed(out.splitlines()) if l.strip()), "")
            return None, "O Windows não concluiu a operação." + (f" Detalhe: {last}" if last else "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
