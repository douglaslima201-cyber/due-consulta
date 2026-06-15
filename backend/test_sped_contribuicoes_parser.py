"""
Smoke test do parser de SPED Contribuições usando o arquivo de exemplo
sintético (backend/sped_contribuicoes_exemplo.txt).

Executar: python test_sped_contribuicoes_parser.py
"""
import os

from sped_contribuicoes_parser import (
    parse_sped_file, extract_header, build_item_map, build_conta_map,
    build_participante_map, parse_decimal,
)

CAMINHO = os.path.join(os.path.dirname(__file__), "sped_contribuicoes_exemplo.txt")


def main():
    with open(CAMINHO, "rb") as f:
        conteudo = f.read()

    dfs = parse_sped_file(conteudo)

    print("Registros encontrados:", sorted(dfs.keys()))
    print()

    header = extract_header(dfs)
    print("Cabeçalho:", header)
    print()

    item_map = build_item_map(dfs)
    conta_map = build_conta_map(dfs)
    part_map = build_participante_map(dfs)
    print("Itens (0200):", item_map)
    print("Contas (0500):", conta_map)
    print("Participantes (0150):", part_map)
    print()

    print("--- A170 ---")
    print(dfs["A170"][["cod_item", "cst_pis", "vl_bc_pis", "vl_pis", "cod_cta"]])
    print()

    print("--- C170 ---")
    print(dfs["C170"][["cod_item", "cst_pis", "vl_bc_pis", "vl_pis", "cod_cta"]])
    print()

    print("--- F100 ---")
    print(dfs["F100"][["cod_item", "vl_oper", "vl_bc_pis", "aliq_pis", "vl_pis", "cod_cta"]])
    print()

    print("--- F130 ---")
    print(dfs["F130"][["nat_bc_cred", "ident_bem_imob", "vl_oper_aquis", "cst_pis", "vl_bc_pis", "vl_pis", "cod_cta"]])
    print()

    print("--- M105 ---")
    print(dfs["M105"])
    print()

    # Conferências cruzadas
    total_vl_pis_acf = (
        parse_decimal(dfs["A170"]["vl_pis"].iloc[0])
        + dfs["C170"]["vl_pis"].apply(parse_decimal).sum()
        + dfs["F100"]["vl_pis"].apply(parse_decimal).sum()
    )
    print(f"Soma VL_PIS (A170+C170+F100) = {total_vl_pis_acf:.2f}")

    total_m100_pis = dfs["M100"]["vl_cred_apur"].apply(parse_decimal).sum()
    print(f"Soma VL_CRED_APUR (M100)      = {total_m100_pis:.2f}")
    print(f"Diferença (esperado: ~ crédito rateado F130) = {total_m100_pis - total_vl_pis_acf:.2f}")

    pct_trib = header["receita_bruta"]["pct_trib_mi"]
    print(f"\n% rateio (receita tributada / total) = {pct_trib * 100:.2f}%")
    vl_bc_f130 = parse_decimal(dfs["F130"]["vl_bc_pis"].iloc[0])
    print(f"VL_BC_PIS F130 (pré-rateio) = {vl_bc_f130:.2f}")
    print(f"VL_BC_PIS F130 rateado esperado = {vl_bc_f130 * pct_trib:.2f}")
    print(f"VL_BC_PIS M105 (nat_bc_cred=10, CST 56) = {dfs['M105'].iloc[2]['vl_bc_pis']}")


if __name__ == "__main__":
    main()
