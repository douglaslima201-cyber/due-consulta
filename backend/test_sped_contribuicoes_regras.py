"""
Smoke test do motor de regras G1-G5 usando o arquivo de exemplo sintético.

Executar: python test_sped_contribuicoes_regras.py
"""
import os
import json

from sped_contribuicoes_parser import parse_sped_file, extract_header
from sped_contribuicoes_regras import gerar_achados

CAMINHO = os.path.join(os.path.dirname(__file__), "sped_contribuicoes_exemplo.txt")


def main():
    with open(CAMINHO, "rb") as f:
        conteudo = f.read()

    dfs = parse_sped_file(conteudo)
    header = extract_header(dfs)
    achados = gerar_achados(dfs, header)

    print(f"Total de achados: {len(achados)}\n")
    for a in achados:
        print(f"[{a['grupo']}] {a['tipo']:15s} ({a['severidade']:6s}) "
              f"{a['registro']:5s} R$ {a['valor_envolvido']:>12,.2f} | {a['descricao']}")
        print(f"        -> {a['recomendacao']}")
        print(f"        base legal: {a['base_legal']}")
        print()


if __name__ == "__main__":
    main()
