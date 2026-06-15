"""
Parser de arquivos SPED Contribuições (EFD-PIS/COFINS).

Layout dos registros validado a partir de arquivo real (Guia Prático
EFD-Contribuições). Cada linha do SPED tem o formato:

    |REG|CAMPO1|CAMPO2|...|CAMPON|

O parser tokeniza cada linha, agrupa por registro (REG) e monta um
DataFrame por registro, usando os nomes de campo definidos em
``REGISTROS``. Registros sem layout definido recebem nomes genéricos
(``campo_1``, ``campo_2``, ...) para não perder informação.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

import pandas as pd


# ─── 1. LAYOUTS DOS REGISTROS ─────────────────────────────────────────────────
# Nomes dos campos na ordem em que aparecem APÓS o código do registro (REG).
# "_extra" marca um campo presente no arquivo real cujo significado exato
# não é necessário para o motor de regras (mantido para não perder coluna).

REGISTROS: dict[str, list[str]] = {
    # Bloco 0 — Abertura, identificação e referências
    "0000": [
        "cod_ver", "tipo_escrit", "ind_sit_esp", "num_rec_anterior",
        "dt_ini", "dt_fin", "nome", "cnpj", "uf", "cod_mun", "suframa",
        "ind_nat_pj", "ind_ativ",
    ],
    "0110": ["cod_inc_trib", "ind_apro_cred", "cod_tipo_cont", "ind_reg_cum"],
    "0111": [
        "rec_bru_ncum_trib_mi", "rec_bru_ncum_nt_mi", "rec_bru_ncum_exp",
        "rec_bru_cum", "rec_bru_total",
    ],
    "0150": [
        "cod_part", "nome", "cod_pais", "cnpj", "cpf", "ie", "cod_mun",
        "suframa", "endereco", "num", "compl", "bairro",
    ],
    "0200": [
        "cod_item", "descr_item", "cod_barra", "cod_ant_item", "unid_inv",
        "tipo_item", "cod_ncm", "ex_ipi", "cod_gen", "cod_lst", "aliq_icms",
    ],
    "0400": ["cod_nat", "descr_nat"],
    "0500": [
        "dt_alt", "cod_nat_cc", "ind_cta", "nivel", "cod_cta", "nome_cta",
        "cod_cta_sup", "centro_custo",
    ],

    # Bloco A — Serviços tomados (ISS)
    "A100": [
        "ind_oper", "_extra", "cod_part", "cod_sit", "ser", "sub", "num_doc",
        "chv_nfe", "dt_doc", "dt_e_s", "vl_doc", "ind_pgto", "vl_desc",
        "vl_bc_pis", "vl_pis", "vl_bc_cofins", "vl_cofins", "vl_pis_ret",
        "vl_cofins_ret", "vl_iss",
    ],
    "A170": [
        "num_item", "cod_item", "descr_compl", "vl_item", "vl_desc",
        "nat_bc_cred", "ind_orig_cred", "cst_pis", "vl_bc_pis", "aliq_pis",
        "vl_pis", "cst_cofins", "vl_bc_cofins", "aliq_cofins", "vl_cofins",
        "cod_cta", "cod_ccus",
    ],

    # Bloco C — Documentos fiscais (mercadorias)
    "C100": [
        "ind_oper", "_extra", "cod_part", "cod_mod", "cod_sit", "ser",
        "num_doc", "chv_nfe", "dt_doc", "dt_e_s", "vl_doc", "ind_pgto",
        "vl_desc", "vl_abat_nt", "vl_merc", "ind_frt", "vl_frt", "vl_seg",
        "vl_out_da", "vl_bc_icms", "vl_icms", "vl_bc_icms_st", "vl_icms_st",
        "vl_ipi", "vl_pis", "vl_cofins", "vl_pis_st", "vl_cofins_st",
    ],
    "C170": [
        "num_item", "cod_item", "descr_compl", "qtd", "unid", "vl_item",
        "vl_desc", "ind_mov", "cst_icms", "cfop", "cod_nat", "vl_bc_icms",
        "aliq_icms", "vl_icms", "vl_bc_icms_st", "aliq_st", "vl_icms_st",
        "ind_apur", "cst_ipi", "cod_enq", "vl_bc_ipi", "aliq_ipi", "vl_ipi",
        "cst_pis", "vl_bc_pis", "aliq_pis", "quant_bc_pis",
        "aliq_pis_quant", "vl_pis", "cst_cofins", "vl_bc_cofins",
        "aliq_cofins", "quant_bc_cofins", "aliq_cofins_quant", "vl_cofins",
        "cod_cta", "cod_ccus",
    ],

    # Bloco D — Serviços de transporte (CT-e)
    "D100": [
        "ind_oper", "_extra", "cod_part", "cod_mod", "cod_sit", "ser",
        "sub", "num_doc", "chv_cte", "dt_doc", "dt_a_p", "tp_ct_e",
        "chv_cte_ref", "vl_doc", "vl_desc", "ind_frt", "vl_serv",
        "vl_bc_icms", "vl_icms", "vl_nt", "cod_inf", "cod_cta",
    ],
    "D200": [
        "ind_oper", "ind_doc", "qtd_doc", "cfop", "vl_doc", "vl_desc",
        "vl_bc_icms", "vl_icms", "vl_nt", "cod_cta",
    ],
    "D205": ["dt_doc_ini", "dt_doc_fim", "vl_doc", "vl_desc"],

    # Bloco F — Demais operações com incidência de PIS/COFINS
    "F100": [
        "ind_oper", "cod_part", "cod_item", "dt_oper", "vl_oper", "cst_pis",
        "vl_bc_pis", "aliq_pis", "vl_pis", "cst_cofins", "vl_bc_cofins",
        "aliq_cofins", "vl_cofins", "nat_bc_cred", "ind_orig_cred",
        "cod_cta", "cod_ccus", "desc_doc_oper",
    ],
    # F120/F129 — créditos de encargos de depreciação (ativo imobilizado)
    "F120": [
        "nat_bc_cred", "ident_bem_imob", "ind_orig_cred", "ind_util_bem_imob",
        "mes_oper_aquis", "vl_oper_depre", "parc_oper_nao_bc_cred",
        "vl_bc_cred", "cst_pis", "vl_bc_pis", "aliq_pis", "vl_pis",
        "cst_cofins", "vl_bc_cofins", "aliq_cofins", "vl_cofins",
        "cod_cta", "cod_ccus",
    ],
    # F130/F139 — créditos sobre valor de aquisição (ativo imobilizado / frota)
    "F130": [
        "nat_bc_cred", "ident_bem_imob", "ind_orig_cred", "ind_util_bem_imob",
        "mes_oper_aquis", "vl_oper_aquis", "parc_oper_nao_bc_cred",
        "vl_bc_cred", "num_parc", "cst_pis", "vl_bc_pis", "aliq_pis",
        "vl_pis", "cst_cofins", "vl_bc_cofins", "aliq_cofins", "vl_cofins",
        "cod_cta", "cod_ccus",
    ],

    # Bloco M — Apuração PIS (M1xx/M2xx) e COFINS (M5xx/M6xx)
    "M100": [
        "cod_cred", "ind_cred_ori", "vl_bc_cont", "aliq_pis",
        "quant_bc_cont", "aliq_pis_quant", "vl_cred_apur",
        "vl_cred_desc_ant", "vl_cred_per", "vl_cred_dcomp", "vl_cred_disp",
        "vl_cred_desc_pa", "vl_cred_desc_tot", "vl_cred_outras",
    ],
    "M105": [
        "nat_bc_cred", "cst_pis", "vl_bc_pis_tot", "vl_ajus_acres_bc_pis",
        "vl_ajus_reduc_bc_pis", "vl_bc_pis", "vl_bc_pis_ef",
        "perc_rat_cred", "vl_cred_dif", "vl_cred_disp_descontar",
    ],
    "M200": [
        "vl_tot_cont_nc_per", "vl_tot_cred_desc", "vl_tot_cont_nc_dev",
        "vl_ret_nc", "vl_out_ded_nc", "vl_cont_nc_rec", "vl_tot_cont_cum_per",
        "vl_ret_cum", "vl_out_ded_cum", "vl_cont_cum_rec", "vl_tot_cont_rec",
        "vl_tot_cont_deb",
    ],
    "M210": [
        "cod_cont", "vl_rec_brt", "vl_bc_cont", "vl_ajus_acres",
        "vl_ajus_reduc", "vl_bc_cont_ajus", "aliq_pis", "quant_bc_cont",
        "aliq_pis_quant", "vl_cont_apur", "vl_ajus_acres2", "vl_ajus_reduc2",
        "vl_bc_cont_ajus2", "vl_cont_dif", "vl_cont_dif_ant", "vl_cont_per",
    ],
    "M400": ["cst_pis", "vl_tot_rec", "cod_cta"],
    "M410": ["nat_rec", "vl_rec", "cod_cta"],

    # COFINS — espelho dos registros M1xx/M2xx/M4xx
    "M500": [
        "cod_cred", "ind_cred_ori", "vl_bc_cont", "aliq_cofins",
        "quant_bc_cont", "aliq_cofins_quant", "vl_cred_apur",
        "vl_cred_desc_ant", "vl_cred_per", "vl_cred_dcomp", "vl_cred_disp",
        "vl_cred_desc_pa", "vl_cred_desc_tot", "vl_cred_outras",
    ],
    "M505": [
        "nat_bc_cred", "cst_cofins", "vl_bc_cofins_tot",
        "vl_ajus_acres_bc_cofins", "vl_ajus_reduc_bc_cofins", "vl_bc_cofins",
        "vl_bc_cofins_ef", "perc_rat_cred", "vl_cred_dif",
        "vl_cred_disp_descontar",
    ],
    "M600": [
        "vl_tot_cont_nc_per", "vl_tot_cred_desc", "vl_tot_cont_nc_dev",
        "vl_ret_nc", "vl_out_ded_nc", "vl_cont_nc_rec", "vl_tot_cont_cum_per",
        "vl_ret_cum", "vl_out_ded_cum", "vl_cont_cum_rec", "vl_tot_cont_rec",
        "vl_tot_cont_deb",
    ],
    "M610": [
        "cod_cont", "vl_rec_brt", "vl_bc_cont", "vl_ajus_acres",
        "vl_ajus_reduc", "vl_bc_cont_ajus", "aliq_cofins", "quant_bc_cont",
        "aliq_cofins_quant", "vl_cont_apur", "vl_ajus_acres2",
        "vl_ajus_reduc2", "vl_bc_cont_ajus2", "vl_cont_dif",
        "vl_cont_dif_ant", "vl_cont_per",
    ],
    "M800": ["cst_cofins", "vl_tot_rec", "cod_cta"],
    "M810": ["nat_rec", "vl_rec", "cod_cta"],

    # Bloco 1 — Controle de créditos fiscais (transposição de saldo)
    "1100": [
        "per_apur_cred", "orig_cred", "cst_pis", "cod_cred", "vl_cred",
        "vl_ajus_acres", "vl_ajus_reduc", "vl_cred_desc_pa_ant",
        "vl_cred_desc_pa", "vl_cred_desc_out", "vl_cred_desc_tot",
        "vl_cred_dif", "vl_cred_disp", "vl_cred_per_disp", "vl_cred_dcomp",
        "vl_cred_outras", "sld_cred_final",
    ],
    "1500": [
        "per_apur_cred", "orig_cred", "cst_cofins", "cod_cred", "vl_cred",
        "vl_ajus_acres", "vl_ajus_reduc", "vl_cred_desc_pa_ant",
        "vl_cred_desc_pa", "vl_cred_desc_out", "vl_cred_desc_tot",
        "vl_cred_dif", "vl_cred_disp", "vl_cred_per_disp", "vl_cred_dcomp",
        "vl_cred_outras", "sld_cred_final",
    ],
}

# CSTs de PIS/COFINS que dão direito a crédito (regime não-cumulativo)
CST_COM_CREDITO = {"50", "51", "52", "53", "54", "55", "56", "60", "61", "62", "63", "64", "65", "66"}
# CST 56/66 = crédito vinculado a receitas tributadas e não-tributadas (rateio)
CST_RATEIO = {"56", "66"}
CST_SEM_CREDITO = {"70", "71", "72", "73", "74", "75", "98", "99"}


# ─── 2. UTILITÁRIOS ────────────────────────────────────────────────────────────

def _decode(content: bytes) -> str:
    """SPED costuma vir em Latin-1/CP1252; tenta UTF-8 antes como fallback."""
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("latin-1", errors="replace")


def parse_decimal(valor) -> float:
    """Converte número no formato SPED ('1234,56' ou '1.234,56') para float."""
    if valor is None:
        return 0.0
    s = str(valor).strip()
    if not s:
        return 0.0
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def competencia_from_ddmmyyyy(data: str) -> str:
    """'01012025' -> '01/2025'."""
    s = str(data).strip()
    if len(s) != 8 or not s.isdigit():
        return ""
    return f"{s[2:4]}/{s[4:8]}"


# ─── 3. PARSER PRINCIPAL ───────────────────────────────────────────────────────

def parse_sped_file(content: bytes) -> dict[str, pd.DataFrame]:
    """Lê o conteúdo de um arquivo SPED Contribuições e retorna um DataFrame
    por registro encontrado, indexado pelo código do registro (ex.: 'C170')."""
    text = _decode(content)

    registros: dict[str, list[list[str]]] = defaultdict(list)
    for linha in text.splitlines():
        linha = linha.strip("\r\n").strip()
        if not linha.startswith("|"):
            continue
        campos = linha.split("|")
        # remove o elemento vazio antes do primeiro '|' e depois do último '|'
        campos = campos[1:-1] if len(campos) >= 2 else campos
        if not campos:
            continue
        reg = campos[0].strip()
        if not reg:
            continue
        registros[reg].append(campos[1:])

    dfs: dict[str, pd.DataFrame] = {}
    for reg, linhas in registros.items():
        max_len = max(len(l) for l in linhas)
        cols = REGISTROS.get(reg)
        if cols is None:
            cols = [f"campo_{i + 1}" for i in range(max_len)]
        elif len(cols) < max_len:
            cols = cols + [f"campo_extra_{i + 1}" for i in range(max_len - len(cols))]

        n = len(cols)
        linhas_padded = [l + [""] * (n - len(l)) if len(l) < n else l[:n] for l in linhas]
        dfs[reg] = pd.DataFrame(linhas_padded, columns=cols)

    return dfs


def extract_header(dfs: dict[str, pd.DataFrame]) -> dict:
    """Extrai cabeçalho (empresa, período, regime de apuração e receita bruta
    do registro 0111, base do rateio proporcional)."""
    header: dict = {}

    df0000 = dfs.get("0000")
    if df0000 is not None and not df0000.empty:
        row = df0000.iloc[0]
        header["razao_social"] = row.get("nome", "")
        header["cnpj"] = row.get("cnpj", "")
        header["uf"] = row.get("uf", "")
        header["dt_ini"] = row.get("dt_ini", "")
        header["dt_fin"] = row.get("dt_fin", "")
        header["competencia"] = competencia_from_ddmmyyyy(row.get("dt_ini", ""))

    df0110 = dfs.get("0110")
    if df0110 is not None and not df0110.empty:
        row = df0110.iloc[0]
        header["cod_inc_trib"] = row.get("cod_inc_trib", "")
        header["ind_apro_cred"] = row.get("ind_apro_cred", "")
        header["rateio_proporcional"] = row.get("ind_apro_cred", "") in ("2", "3")

    df0111 = dfs.get("0111")
    if df0111 is not None and not df0111.empty:
        row = df0111.iloc[0]
        trib = parse_decimal(row.get("rec_bru_ncum_trib_mi", "0"))
        nt = parse_decimal(row.get("rec_bru_ncum_nt_mi", "0"))
        exp = parse_decimal(row.get("rec_bru_ncum_exp", "0"))
        cum = parse_decimal(row.get("rec_bru_cum", "0"))
        total = parse_decimal(row.get("rec_bru_total", "0"))
        header["receita_bruta"] = {
            "trib_mi": trib, "nt_mi": nt, "exp": exp, "cum": cum, "total": total,
            "pct_trib_mi": (trib / total) if total else 0.0,
            "pct_nt_mi": (nt / total) if total else 0.0,
            "pct_exp": (exp / total) if total else 0.0,
            "pct_cum": (cum / total) if total else 0.0,
        }

    return header


# ─── 4. MAPAS DE REFERÊNCIA (cruzamento entre registros) ───────────────────────

def build_item_map(dfs: dict[str, pd.DataFrame]) -> dict[str, str]:
    """cod_item -> descr_item (registro 0200)."""
    df = dfs.get("0200")
    if df is None or df.empty:
        return {}
    return dict(zip(df["cod_item"], df["descr_item"]))


def build_conta_map(dfs: dict[str, pd.DataFrame]) -> dict[str, str]:
    """cod_cta -> nome_cta (registro 0500 — plano de contas)."""
    df = dfs.get("0500")
    if df is None or df.empty:
        return {}
    return dict(zip(df["cod_cta"], df["nome_cta"]))


def build_participante_map(dfs: dict[str, pd.DataFrame]) -> dict[str, str]:
    """cod_part -> nome (registro 0150 — participantes/fornecedores)."""
    df = dfs.get("0150")
    if df is None or df.empty:
        return {}
    return dict(zip(df["cod_part"], df["nome"]))
