"""Credenciais de DESENVOLVEDOR embutidas no build do NeoSD (ScreenScraper: devid/devpassword).

Elas nunca ficam no código-fonte: o build.bat roda tools/embutir_chaves.py, que lê o arquivo chaves.local.json
(na pasta do projeto, fora do git) ou as variáveis NEOSD_SS_DEVID / NEOSD_SS_DEVPASSWORD e gera
core/scraper/_builtin_keys.py só para o .exe. Num clone do projeto sem esse arquivo, nada vem embutido e o usuário
pode preencher as próprias credenciais de desenvolvedor.

Os valores ficam em hexadecimal no .exe só para não aparecerem como texto puro. Não é segredo de verdade (nenhum
programa que roda no PC do usuário consegue esconder uma credencial por completo); é o mesmo modelo dos outros
frontends, e o ScreenScraper controla o uso pela conta de cada usuário.
(Antes era base64 + XOR: o antivírus do Windows bloqueou este arquivo por causa desse padrão, e o NeoSD não abria.)
"""


def mask(text):
    return (text or "").encode("utf-8").hex()


def unmask(text):
    return bytes.fromhex(text or "").decode("utf-8")


def screenscraper():
    """-> (devid, devpassword) embutidos no build, ou None."""
    try:
        from ._builtin_keys import SS  # gerado pelo build; não existe no código-fonte
    except ImportError:
        return None
    try:
        devid, devpw = unmask(SS[0]), unmask(SS[1])
    except Exception:
        return None
    return (devid, devpw) if devid and devpw else None
