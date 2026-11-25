"""Gera core/scraper/_builtin_keys.py para o build (chamado pelo build.bat e pelo GitHub Actions).

De onde vêm as credenciais de desenvolvedor do ScreenScraper (a primeira que existir):
  1. variáveis de ambiente NEOSD_SS_DEVID e NEOSD_SS_DEVPASSWORD (no GitHub: Settings → Secrets);
  2. o arquivo chaves.local.json na pasta do projeto:  {"screenscraper": {"devid": "...", "devpassword": "..."}}
Os dois ficam FORA do git (.gitignore). Sem nenhum deles, o NeoSD é gerado sem credenciais embutidas.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.scraper.devkeys import mask  # noqa: E402

OUT = ROOT / "core" / "scraper" / "_builtin_keys.py"


def main():
    devid, devpw = os.environ.get("NEOSD_SS_DEVID", ""), os.environ.get("NEOSD_SS_DEVPASSWORD", "")
    src = "variáveis de ambiente"
    if not (devid and devpw):
        f = ROOT / "chaves.local.json"
        if f.is_file():
            ss = (json.loads(f.read_text(encoding="utf-8-sig")) or {}).get("screenscraper") or {}
            devid, devpw, src = str(ss.get("devid", "")).strip(), str(ss.get("devpassword", "")).strip(), f.name
    if "COLE_AQUI" in devid + devpw:  # o modelo foi só renomeado, sem trocar os textos
        print("ScreenScraper: chaves.local.json ainda tem os textos do modelo (COLE_AQUI...). Troque pelos seus dados.")
        devid = devpw = ""
    if devid and devpw:
        OUT.write_text("# Gerado pelo build (tools/embutir_chaves.py). Não versionar.\n"
                       f"SS = ({mask(devid)!r}, {mask(devpw)!r})\n", encoding="utf-8")
        print(f"ScreenScraper: credenciais de desenvolvedor embutidas ({src}).")
    else:
        OUT.unlink(missing_ok=True)
        print("ScreenScraper: sem credenciais de desenvolvedor (crie chaves.local.json para embutir).")


if __name__ == "__main__":
    main()
