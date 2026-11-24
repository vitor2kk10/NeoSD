"""Sobe o número do arquivo VERSION seguindo a regra do NeoSD.

Regra: cada atualização de correções soma 2 no último número (1.7.2 -> 1.7.4 -> 1.7.6 -> 1.7.8).
Quando o último número chegaria a 10, ele volta a 0 e o do meio sobe: 1.7.8 -> 1.8.0.
(O do meio segue a mesma ideia: 1.9.8 -> 2.0.0.)

    python tools/proxima_versao.py           mostra a atual e a próxima
    python tools/proxima_versao.py --gravar  grava a próxima no VERSION
"""
import sys
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "VERSION"


def proxima(ver):
    major, minor, patch = (int(x) for x in (ver.strip().split(".") + ["0", "0"])[:3])
    patch += 2
    if patch >= 10:
        patch, minor = 0, minor + 1
    if minor >= 10:
        minor, major = 0, major + 1
    return f"{major}.{minor}.{patch}"


if __name__ == "__main__":
    atual = FILE.read_text(encoding="utf-8").strip()
    nova = proxima(atual)
    if "--gravar" in sys.argv:
        FILE.write_text(nova + "\n", encoding="utf-8")
        print(f"VERSION: {atual} -> {nova}")
    else:
        print(f"Atual: {atual}   Próxima: {nova}   (use --gravar para salvar)")
