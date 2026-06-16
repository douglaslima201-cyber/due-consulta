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

    # Bloco D — Serviços de transporte (CT-e / NFST)
    "D100": [
        "ind_oper", "ind_emit", "cod_part", "cod_mod", "cod_sit", "ser",
        "sub", "num_doc", "chv_cte", "dt_doc", "dt_a_p", "tp_ct_e",
        "chv_cte_ref", "vl_doc", "vl_desc", "ind_frt", "vl_serv",
        "vl_bc_icms", "vl_icms", "vl_nt", "cod_inf", "cod_cta",
    ],
    # D101/D105 — crédito presumido PIS/COFINS (filhos de D100)
    "D101": [
        "nat_bc_cred", "cst_pis", "vl_item_nc", "vl_bc_cred",
        "aliq_pis", "vl_cred", "cod_cta", "cod_ccus",
    ],
    "D105": [
        "nat_bc_cred", "cst_cofins", "vl_item_nc", "vl_bc_cred",
        "aliq_cofins", "vl_cred", "cod_cta", "cod_ccus",
    ],
    "D200": [
        "ind_oper", "ind_doc", "qtd_doc", "cfop", "vl_doc", "vl_desc",
        "vl_bc_icms", "vl_icms", "vl_nt", "cod_cta",
    ],
    # D201/D205 — crédito presumido PIS/COFINS (filhos de D200, análogos a D101/D105)
    "D201": [
        "nat_bc_cred", "cst_pis", "vl_item_nc", "vl_bc_cred",
        "aliq_pis", "vl_cred", "cod_cta", "cod_ccus",
    ],
    "D205": [
        "nat_bc_cred", "cst_cofins", "vl_item_nc", "vl_bc_cred",
        "aliq_cofins", "vl_cred", "cod_cta", "cod_ccus",
    ],

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

# Colunas extras injetadas durante o parse para manter contexto do registro-pai
_EXTRA_COLS: dict[str, list[str]] = {
    "C170": ["_ind_oper"],
    "A170": ["_ind_oper"],
}

# Labels para IND_OPER (C100/A100)
_IND_OPER_LABEL = {"0": "Entrada", "1": "Saída"}

# Mapa cod_cont M210/M610 → descrição legível (SPED Contribuições Guia Prático)
COD_CONT_LABEL: dict[str, str] = {
    "01": "Operação Tributável (Alíquota Básica 0,65% / 3%)",
    "02": "Operação Tributável (Alíquota Diferenciada)",
    "03": "Operação Tributável (Alíquota por Unidade de Produto)",
    "04": "Operação Tributável (Mono. / ST — Antecipação)",
    "05": "Operação Tributável (Substituição Tributária)",
    "06": "Operação Tributável (Alíquota Zero / Isenta)",
    "07": "Operação Isenta da Contribuição",
    "08": "Operação sem Incidência (Não-Tributável)",
    "09": "Operação com Suspensão",
    "49": "Outras Operações de Saída",
    "99": "Outras Operações",
}


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
    por registro encontrado, indexado pelo código do registro (ex.: 'C170').

    Após a construção dos DataFrames, injeta a coluna ``_ind_oper`` em C170
    (derivada do C100 pai) e em A170 (derivada do A100 pai), permitindo
    identificar entradas vs saídas sem desalinhar os outros campos."""
    text = _decode(content)

    registros: dict[str, list[list[str]]] = defaultdict(list)
    # Armazena, por linha, o valor de _ind_oper a ser anexado depois
    _ind_oper_extra: dict[str, list[str]] = {
        "C170": [], "A170": [], "D101": [], "D105": [], "D201": [], "D205": [],
    }
    _cur_ind_oper: dict[str, str] = {}

    for linha in text.splitlines():
        linha = linha.strip("\r\n").strip()
        if not linha.startswith("|"):
            continue
        campos = linha.split("|")
        campos = campos[1:-1] if len(campos) >= 2 else campos
        if not campos:
            continue
        reg = campos[0].strip()
        if not reg:
            continue
        valores = campos[1:]

        if reg in ("C100", "A100", "D100", "D200"):
            _cur_ind_oper[reg] = valores[0] if valores else ""
        elif reg == "C170":
            _ind_oper_extra["C170"].append(_cur_ind_oper.get("C100", ""))
        elif reg == "A170":
            _ind_oper_extra["A170"].append(_cur_ind_oper.get("A100", ""))
        elif reg == "D101":
            _ind_oper_extra["D101"].append(_cur_ind_oper.get("D100", ""))
        elif reg == "D105":
            _ind_oper_extra["D105"].append(_cur_ind_oper.get("D100", ""))
        elif reg == "D201":
            _ind_oper_extra["D201"].append(_cur_ind_oper.get("D200", ""))
        elif reg == "D205":
            _ind_oper_extra["D205"].append(_cur_ind_oper.get("D200", ""))

        registros[reg].append(valores)

    dfs: dict[str, pd.DataFrame] = {}
    for reg, linhas in registros.items():
        max_len = max(len(l) for l in linhas)
        base_cols = REGISTROS.get(reg)

        if base_cols is None:
            cols: list[str] = [f"campo_{i + 1}" for i in range(max_len)]
        else:
            cols = list(base_cols)
            needed = max_len - len(cols)
            if needed > 0:
                cols = cols + [f"campo_extra_{i + 1}" for i in range(needed)]

        n = len(cols)
        linhas_padded = [l + [""] * (n - len(l)) if len(l) < n else l[:n] for l in linhas]
        dfs[reg] = pd.DataFrame(linhas_padded, columns=cols)

    # Injeta _ind_oper como coluna extra (após construção, sem desalinhar outros campos)
    for reg, extra_vals in _ind_oper_extra.items():
        if reg in dfs and extra_vals:
            dfs[reg]["_ind_oper"] = extra_vals

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


def build_participante_tipo_map(dfs: dict[str, pd.DataFrame]) -> dict[str, str]:
    """cod_part -> 'PF' se CPF preenchido, 'PJ' caso contrário (registro 0150).
    Usado para detectar subcontratação de autônomos (PF) no G1."""
    df = dfs.get("0150")
    if df is None or df.empty:
        return {}
    result = {}
    for _, row in df.iterrows():
        cod = str(row.get("cod_part", "")).strip()
        cpf = str(row.get("cpf", "")).strip().replace(".", "").replace("-", "")
        result[cod] = "PF" if cpf and len(cpf) >= 11 and cpf not in ("00000000000",) else "PJ"
    return result


# ─── 5. EXTRATORES COMPLEMENTARES ─────────────────────────────────────────────

def extract_apuracao(dfs: dict[str, pd.DataFrame]) -> dict:
    """Extrai a totalização da apuração do Bloco M:
    - M200 (PIS totais) + M210 (PIS por código de contribuição / CST)
    - M600 (COFINS totais) + M610 (COFINS por código de contribuição)
    Retorna dict com sub-dicts 'pis' e 'cofins'."""

    def _totais(df_tot, df_det, df_m100, df_controle, campo_aliq: str) -> dict:
        out: dict = {
            "total_contribuicao_periodo": 0.0,
            "total_creditos_descontados": 0.0,   # M200/M600 — créditos M100/M500 aplicados no período
            "creditos_periodos_anteriores": 0.0,  # 1100/1500 vl_cred_desc_pa — créditos de meses anteriores usados agora
            "contribuicao_devida": 0.0,
            "valor_a_recolher": 0.0,             # após descontar também os créditos de períodos anteriores
            "credito_total_mes": 0.0,            # soma M100/M500 vl_cred_apur — créditos apurados no mês
            "sobra_mes": 0.0,                    # crédito_total_mes − total_creditos_descontados
            "por_cst": [],
        }
        ret = 0.0
        if df_tot is not None and not df_tot.empty:
            r = df_tot.iloc[0]
            out["total_contribuicao_periodo"] = parse_decimal(r.get("vl_tot_cont_nc_per", "0"))
            out["total_creditos_descontados"] = parse_decimal(r.get("vl_tot_cred_desc", "0"))
            out["contribuicao_devida"] = parse_decimal(r.get("vl_tot_cont_nc_dev", "0"))
            ret = parse_decimal(r.get("vl_ret_nc", "0"))
            ded = parse_decimal(r.get("vl_out_ded_nc", "0"))
            ret += ded

        # Créditos de períodos anteriores: soma de vl_cred_desc_pa em 1100/1500
        if df_controle is not None and not df_controle.empty and "vl_cred_desc_pa" in df_controle.columns:
            out["creditos_periodos_anteriores"] = round(
                df_controle["vl_cred_desc_pa"].apply(parse_decimal).sum(), 2
            )

        # Saldo a pagar = contribuição − créditos do período − créditos de períodos anteriores − retenções
        out["valor_a_recolher"] = round(
            max(0.0, out["total_contribuicao_periodo"]
                - out["total_creditos_descontados"]
                - out["creditos_periodos_anteriores"]
                - ret), 2
        )

        # Crédito total do mês = soma de vl_cred_apur em M100/M500
        if df_m100 is not None and not df_m100.empty and "vl_cred_apur" in df_m100.columns:
            out["credito_total_mes"] = round(
                df_m100["vl_cred_apur"].apply(parse_decimal).sum(), 2
            )

        # Sobra = créditos apurados no mês que não foram usados para quitar a contribuição
        out["sobra_mes"] = round(
            max(0.0, out["credito_total_mes"] - out["total_creditos_descontados"]), 2
        )

        if df_det is not None and not df_det.empty:
            for _, row in df_det.iterrows():
                cod = str(row.get("cod_cont", "")).strip()
                out["por_cst"].append({
                    "cod_cont": cod,
                    "descricao": COD_CONT_LABEL.get(cod, f"Código {cod}"),
                    "vl_rec_brt": parse_decimal(row.get("vl_rec_brt", "0")),
                    "vl_bc_cont": parse_decimal(row.get("vl_bc_cont", "0")),
                    "aliq": parse_decimal(row.get(campo_aliq, "0")),
                    "vl_cont_apur": parse_decimal(row.get("vl_cont_apur", "0")),
                })
        return out

    def _saldos_controle(df) -> list[dict]:
        if df is None or df.empty:
            return []
        result = []
        for _, row in df.iterrows():
            vl_cred = parse_decimal(row.get("vl_cred", "0"))
            vl_desc_ant = parse_decimal(row.get("vl_cred_desc_pa_ant", "0"))
            vl_desc_pa = parse_decimal(row.get("vl_cred_desc_pa", "0"))
            vl_disp = parse_decimal(row.get("vl_cred_disp", "0"))
            sld_final = parse_decimal(row.get("sld_cred_final", "0"))
            result.append({
                "per_apur_cred": str(row.get("per_apur_cred", "")).strip(),
                "cod_cred": str(row.get("cod_cred", "")).strip(),
                "vl_cred": round(vl_cred, 2),
                "vl_cred_desc_pa_ant": round(vl_desc_ant, 2),
                "vl_cred_desc_pa": round(vl_desc_pa, 2),
                "vl_cred_disp": round(vl_disp, 2),
                "sld_cred_final": round(sld_final, 2),
            })
        return result

    return {
        "pis": _totais(dfs.get("M200"), dfs.get("M210"), dfs.get("M100"), dfs.get("1100"), "aliq_pis"),
        "cofins": _totais(dfs.get("M600"), dfs.get("M610"), dfs.get("M500"), dfs.get("1500"), "aliq_cofins"),
        "saldos_pis_1100": _saldos_controle(dfs.get("1100")),
        "saldos_cofins_1500": _saldos_controle(dfs.get("1500")),
    }


def extract_cfop_cst(dfs: dict[str, pd.DataFrame]) -> list[dict]:
    """Agrega os valores de PIS/COFINS por CFOP e CST, separando entradas e
    saídas, a partir dos registros C170 (mercadorias), A170 (serviços) e
    D100/D101/D105 (CT-e/NFST — serviços de transporte do Bloco D)."""
    linhas: list[dict] = []

    numeric_cols = ("vl_item", "vl_bc_pis", "vl_pis", "vl_bc_cofins", "vl_cofins")

    df_c170 = dfs.get("C170")
    if df_c170 is not None and not df_c170.empty and "_ind_oper" in df_c170.columns:
        df = df_c170.copy()
        for col in numeric_cols:
            if col in df.columns:
                df[col] = df[col].map(parse_decimal)
            else:
                df[col] = 0.0
        grp_cols = ["_ind_oper", "cfop", "cst_pis", "cst_cofins"]
        grp_cols = [c for c in grp_cols if c in df.columns]
        grouped = df.groupby(grp_cols, dropna=False).agg(
            qtd_itens=("vl_item", "size"),
            vl_item=("vl_item", "sum"),
            vl_bc_pis=("vl_bc_pis", "sum"),
            vl_pis=("vl_pis", "sum"),
            vl_bc_cofins=("vl_bc_cofins", "sum"),
            vl_cofins=("vl_cofins", "sum"),
        ).reset_index()
        for _, row in grouped.iterrows():
            io = str(row.get("_ind_oper", "")).strip()
            linhas.append({
                "origem": "C170",
                "ind_oper": _IND_OPER_LABEL.get(io, io or "—"),
                "cfop": str(row.get("cfop", "")).strip() or "—",
                "cst_pis": str(row.get("cst_pis", "")).strip(),
                "cst_cofins": str(row.get("cst_cofins", "")).strip(),
                "qtd_itens": int(row.get("qtd_itens", 0)),
                "vl_item": round(float(row.get("vl_item", 0)), 2),
                "vl_bc_pis": round(float(row.get("vl_bc_pis", 0)), 2),
                "vl_pis": round(float(row.get("vl_pis", 0)), 2),
                "vl_bc_cofins": round(float(row.get("vl_bc_cofins", 0)), 2),
                "vl_cofins": round(float(row.get("vl_cofins", 0)), 2),
            })

    df_a170 = dfs.get("A170")
    if df_a170 is not None and not df_a170.empty and "_ind_oper" in df_a170.columns:
        df = df_a170.copy()
        for col in numeric_cols:
            if col in df.columns:
                df[col] = df[col].map(parse_decimal)
            else:
                df[col] = 0.0
        grp_cols = ["_ind_oper", "cst_pis", "cst_cofins"]
        grp_cols = [c for c in grp_cols if c in df.columns]
        grouped = df.groupby(grp_cols, dropna=False).agg(
            qtd_itens=("vl_item", "size"),
            vl_item=("vl_item", "sum"),
            vl_bc_pis=("vl_bc_pis", "sum"),
            vl_pis=("vl_pis", "sum"),
            vl_bc_cofins=("vl_bc_cofins", "sum"),
            vl_cofins=("vl_cofins", "sum"),
        ).reset_index()
        for _, row in grouped.iterrows():
            io = str(row.get("_ind_oper", "")).strip()
            linhas.append({
                "origem": "A170",
                "ind_oper": _IND_OPER_LABEL.get(io, io or "—"),
                "cfop": "—",
                "cst_pis": str(row.get("cst_pis", "")).strip(),
                "cst_cofins": str(row.get("cst_cofins", "")).strip(),
                "qtd_itens": int(row.get("qtd_itens", 0)),
                "vl_item": round(float(row.get("vl_item", 0)), 2),
                "vl_bc_pis": round(float(row.get("vl_bc_pis", 0)), 2),
                "vl_pis": round(float(row.get("vl_pis", 0)), 2),
                "vl_bc_cofins": round(float(row.get("vl_bc_cofins", 0)), 2),
                "vl_cofins": round(float(row.get("vl_cofins", 0)), 2),
            })

    # D100 — documentos de transporte (CT-e / NFST); ind_oper é campo próprio do registro
    df_d100 = dfs.get("D100")
    if df_d100 is not None and not df_d100.empty and "ind_oper" in df_d100.columns:
        df = df_d100.copy()
        for col in ("vl_serv", "vl_doc"):
            if col in df.columns:
                df[col] = df[col].map(parse_decimal)
            else:
                df[col] = 0.0
        # vl_serv é o valor da prestação; usa vl_doc como fallback quando zero
        df["_vl"] = df["vl_serv"].where(df["vl_serv"] > 0, df["vl_doc"])
        grp_cols = ["ind_oper", "cod_mod"]
        grp_cols = [c for c in grp_cols if c in df.columns]
        grouped = df.groupby(grp_cols, dropna=False).agg(
            qtd_itens=("_vl", "size"),
            vl_item=("_vl", "sum"),
        ).reset_index()
        for _, row in grouped.iterrows():
            io = str(row.get("ind_oper", "")).strip()
            cod_mod = str(row.get("cod_mod", "")).strip()
            linhas.append({
                "origem": "D100",
                "ind_oper": _IND_OPER_LABEL.get(io, io or "—"),
                "cfop": "—",
                "cst_pis": "—",
                "cst_cofins": f"Mod.{cod_mod}" if cod_mod else "—",
                "qtd_itens": int(row.get("qtd_itens", 0)),
                "vl_item": round(float(row.get("vl_item", 0)), 2),
                "vl_bc_pis": 0.0,
                "vl_pis": 0.0,
                "vl_bc_cofins": 0.0,
                "vl_cofins": 0.0,
            })

    # D101 — crédito presumido PIS (filho de D100)
    df_d101 = dfs.get("D101")
    if df_d101 is not None and not df_d101.empty and "_ind_oper" in df_d101.columns:
        df = df_d101.copy()
        for col in ("vl_item_nc", "vl_bc_cred", "vl_cred"):
            if col in df.columns:
                df[col] = df[col].map(parse_decimal)
            else:
                df[col] = 0.0
        grp_cols = ["_ind_oper", "cst_pis"]
        grp_cols = [c for c in grp_cols if c in df.columns]
        grouped = df.groupby(grp_cols, dropna=False).agg(
            qtd_itens=("vl_item_nc", "size"),
            vl_item=("vl_item_nc", "sum"),
            vl_bc_pis=("vl_bc_cred", "sum"),
            vl_pis=("vl_cred", "sum"),
        ).reset_index()
        for _, row in grouped.iterrows():
            io = str(row.get("_ind_oper", "")).strip()
            linhas.append({
                "origem": "D101",
                "ind_oper": _IND_OPER_LABEL.get(io, io or "—"),
                "cfop": "—",
                "cst_pis": str(row.get("cst_pis", "")).strip(),
                "cst_cofins": "—",
                "qtd_itens": int(row.get("qtd_itens", 0)),
                "vl_item": round(float(row.get("vl_item", 0)), 2),
                "vl_bc_pis": round(float(row.get("vl_bc_pis", 0)), 2),
                "vl_pis": round(float(row.get("vl_pis", 0)), 2),
                "vl_bc_cofins": 0.0,
                "vl_cofins": 0.0,
            })

    # D105 — crédito presumido COFINS (filho de D100)
    df_d105 = dfs.get("D105")
    if df_d105 is not None and not df_d105.empty and "_ind_oper" in df_d105.columns:
        df = df_d105.copy()
        for col in ("vl_item_nc", "vl_bc_cred", "vl_cred"):
            if col in df.columns:
                df[col] = df[col].map(parse_decimal)
            else:
                df[col] = 0.0
        grp_cols = ["_ind_oper", "cst_cofins"]
        grp_cols = [c for c in grp_cols if c in df.columns]
        grouped = df.groupby(grp_cols, dropna=False).agg(
            qtd_itens=("vl_item_nc", "size"),
            vl_item=("vl_item_nc", "sum"),
            vl_bc_cofins=("vl_bc_cred", "sum"),
            vl_cofins=("vl_cred", "sum"),
        ).reset_index()
        for _, row in grouped.iterrows():
            io = str(row.get("_ind_oper", "")).strip()
            linhas.append({
                "origem": "D105",
                "ind_oper": _IND_OPER_LABEL.get(io, io or "—"),
                "cfop": "—",
                "cst_pis": "—",
                "cst_cofins": str(row.get("cst_cofins", "")).strip(),
                "qtd_itens": int(row.get("qtd_itens", 0)),
                "vl_item": round(float(row.get("vl_item", 0)), 2),
                "vl_bc_pis": 0.0,
                "vl_pis": 0.0,
                "vl_bc_cofins": round(float(row.get("vl_bc_cofins", 0)), 2),
                "vl_cofins": round(float(row.get("vl_cofins", 0)), 2),
            })

    # D200 — Consolidação de NF de Serviços de Transporte (saídas consolidadas com CFOP)
    df_d200 = dfs.get("D200")
    if df_d200 is not None and not df_d200.empty and "ind_oper" in df_d200.columns:
        df = df_d200.copy()
        for col in ("vl_doc", "vl_desc"):
            if col in df.columns:
                df[col] = df[col].map(parse_decimal)
            else:
                df[col] = 0.0
        df["_vl"] = df["vl_doc"] - df["vl_desc"]
        grp_cols = ["ind_oper", "cfop"]
        grp_cols = [c for c in grp_cols if c in df.columns]
        grouped = df.groupby(grp_cols, dropna=False).agg(
            qtd_itens=("_vl", "size"),
            vl_item=("_vl", "sum"),
        ).reset_index()
        for _, row in grouped.iterrows():
            io = str(row.get("ind_oper", "")).strip()
            linhas.append({
                "origem": "D200",
                "ind_oper": _IND_OPER_LABEL.get(io, io or "—"),
                "cfop": str(row.get("cfop", "")).strip() or "—",
                "cst_pis": "—",
                "cst_cofins": "—",
                "qtd_itens": int(row.get("qtd_itens", 0)),
                "vl_item": round(float(row.get("vl_item", 0)), 2),
                "vl_bc_pis": 0.0,
                "vl_pis": 0.0,
                "vl_bc_cofins": 0.0,
                "vl_cofins": 0.0,
            })

    # D201 — crédito presumido PIS (filho de D200, análogo a D101)
    df_d201 = dfs.get("D201")
    if df_d201 is not None and not df_d201.empty and "_ind_oper" in df_d201.columns:
        df = df_d201.copy()
        for col in ("vl_item_nc", "vl_bc_cred", "vl_cred"):
            if col in df.columns:
                df[col] = df[col].map(parse_decimal)
            else:
                df[col] = 0.0
        grp_cols = ["_ind_oper", "cst_pis"]
        grp_cols = [c for c in grp_cols if c in df.columns]
        grouped = df.groupby(grp_cols, dropna=False).agg(
            qtd_itens=("vl_item_nc", "size"),
            vl_item=("vl_item_nc", "sum"),
            vl_bc_pis=("vl_bc_cred", "sum"),
            vl_pis=("vl_cred", "sum"),
        ).reset_index()
        for _, row in grouped.iterrows():
            io = str(row.get("_ind_oper", "")).strip()
            linhas.append({
                "origem": "D201",
                "ind_oper": _IND_OPER_LABEL.get(io, io or "—"),
                "cfop": "—",
                "cst_pis": str(row.get("cst_pis", "")).strip(),
                "cst_cofins": "—",
                "qtd_itens": int(row.get("qtd_itens", 0)),
                "vl_item": round(float(row.get("vl_item", 0)), 2),
                "vl_bc_pis": round(float(row.get("vl_bc_pis", 0)), 2),
                "vl_pis": round(float(row.get("vl_pis", 0)), 2),
                "vl_bc_cofins": 0.0,
                "vl_cofins": 0.0,
            })

    # D205 — crédito presumido COFINS (filho de D200, análogo a D105)
    df_d205 = dfs.get("D205")
    if df_d205 is not None and not df_d205.empty and "_ind_oper" in df_d205.columns:
        df = df_d205.copy()
        for col in ("vl_item_nc", "vl_bc_cred", "vl_cred"):
            if col in df.columns:
                df[col] = df[col].map(parse_decimal)
            else:
                df[col] = 0.0
        grp_cols = ["_ind_oper", "cst_cofins"]
        grp_cols = [c for c in grp_cols if c in df.columns]
        grouped = df.groupby(grp_cols, dropna=False).agg(
            qtd_itens=("vl_item_nc", "size"),
            vl_item=("vl_item_nc", "sum"),
            vl_bc_cofins=("vl_bc_cred", "sum"),
            vl_cofins=("vl_cred", "sum"),
        ).reset_index()
        for _, row in grouped.iterrows():
            io = str(row.get("_ind_oper", "")).strip()
            linhas.append({
                "origem": "D205",
                "ind_oper": _IND_OPER_LABEL.get(io, io or "—"),
                "cfop": "—",
                "cst_pis": "—",
                "cst_cofins": str(row.get("cst_cofins", "")).strip(),
                "qtd_itens": int(row.get("qtd_itens", 0)),
                "vl_item": round(float(row.get("vl_item", 0)), 2),
                "vl_bc_pis": 0.0,
                "vl_pis": 0.0,
                "vl_bc_cofins": round(float(row.get("vl_bc_cofins", 0)), 2),
                "vl_cofins": round(float(row.get("vl_cofins", 0)), 2),
            })

    linhas.sort(key=lambda r: (r["ind_oper"], r["cfop"], r["cst_pis"]))
    return linhas


# ─── 6. PRÉVIA PERDCOMP (Tabela 4.3.6) ────────────────────────────────────────

_TABELA_COD_CRED: dict[str, str] = {
    "101": "Alíquota Básica (1,65% PIS / 7,60% COFINS)",
    "102": "Alíquota Diferenciada",
    "103": "Alíquota por Unidade de Produto",
    "104": "Estoque de Abertura",
    "105": "Embalagens para Revenda",
    "106": "Presumido da Agroindústria",
    "107": "Outras Operações — Básico",
    "108": "Importação de Bens/Serviços como Insumo",
    "109": "Ativo Imob. — Encargos de Depreciação (F120)",
    "110": "Devoluções de Vendas ao Mercado Interno",
    "111": "Outras Operações — Custos e Despesas",
    "112": "Ativo Imob. — Valor de Aquisição (F130)",
    "201": "Presumido — Alíquota Básica (ressarcível / PERDCOMP)",
    "202": "Presumido — Alíquota Diferenciada",
    "203": "Presumido — Alíquota por Unidade de Produto",
    "204": "Presumido — Estoque de Abertura",
    "205": "Presumido — Embalagens para Revenda",
    "206": "Presumido — Agroindústria",
    "207": "Presumido — Outras Operações (ressarcível / PERDCOMP)",
    "208": "Presumido — Importação de Bens/Serviços",
    "209": "Presumido — Ativo Imob. (Depreciação)",
    "210": "Presumido — Devoluções de Vendas",
    "211": "Presumido — Outras Operações",
}


def _sortkey_200(cod: str) -> int:
    """Ordena 200-série para consumo: 207 primeiro, 202 segundo, 201 por último."""
    try:
        c = int(cod)
    except ValueError:
        return 500
    if c == 207:
        return 0
    if c == 202:
        return 1
    if c == 201:
        return 999
    return c


def extract_perdcomp_previa(dfs: dict[str, pd.DataFrame]) -> dict:
    """Classifica créditos do período pelos códigos da Tabela 4.3.6 do SPED e
    simula a ordem ótima de utilização segundo as regras de PERDCOMP:
      1. Série 100 (não ressarcível) — descontada contra contribuição devida
      2. Série 200 (ressarcível)    — ordem: 207 → 202 → demais → 201
    O saldo remanescente no código 201 é o candidato principal ao PERDCOMP."""

    def _build(df_cred, df_tot, tributo: str) -> dict:
        creditos_raw: dict[str, float] = {}
        if df_cred is not None and not df_cred.empty:
            for _, row in df_cred.iterrows():
                cod = str(row.get("cod_cred", "")).strip()
                val = parse_decimal(row.get("vl_cred_disp", "0"))
                if cod:
                    creditos_raw[cod] = creditos_raw.get(cod, 0.0) + val

        contrib_periodo = 0.0
        creditos_descontados_apuracao = 0.0
        if df_tot is not None and not df_tot.empty:
            r = df_tot.iloc[0]
            contrib_periodo = parse_decimal(r.get("vl_tot_cont_nc_per", "0"))
            creditos_descontados_apuracao = parse_decimal(r.get("vl_tot_cred_desc", "0"))

        creditos_100 = [(c, v) for c, v in creditos_raw.items()
                        if c.isdigit() and 100 <= int(c) <= 199]
        creditos_200 = [(c, v) for c, v in creditos_raw.items()
                        if c.isdigit() and 200 <= int(c) <= 299]

        saldo_deve = contrib_periodo
        steps: list[dict] = []

        for cod, valor in sorted(creditos_100, key=lambda x: int(x[0])):
            usado = min(valor, saldo_deve)
            saldo_deve = max(0.0, round(saldo_deve - valor, 2))
            steps.append({
                "cod_cred": cod,
                "descricao": _TABELA_COD_CRED.get(cod, f"Código {cod}"),
                "serie": "100",
                "ressarcivel": False,
                "valor_disponivel": round(valor, 2),
                "valor_usado_desconto": round(usado, 2),
                "valor_perdcomp": 0.0,
                "saldo_contrib_apos": round(max(0.0, saldo_deve), 2),
            })

        for cod, valor in sorted(creditos_200, key=lambda x: _sortkey_200(x[0])):
            usado = min(valor, saldo_deve)
            saldo_deve = max(0.0, round(saldo_deve - valor, 2))
            perdcomp = round(max(0.0, valor - usado), 2)
            steps.append({
                "cod_cred": cod,
                "descricao": _TABELA_COD_CRED.get(cod, f"Código {cod}"),
                "serie": "200",
                "ressarcivel": True,
                "valor_disponivel": round(valor, 2),
                "valor_usado_desconto": round(usado, 2),
                "valor_perdcomp": perdcomp,
                "saldo_contrib_apos": round(max(0.0, saldo_deve), 2),
            })

        total_100 = round(sum(v for _, v in creditos_100), 2)
        total_200 = round(sum(v for _, v in creditos_200), 2)
        total_m100 = round(total_100 + total_200, 2)
        total_perdcomp = round(sum(s["valor_perdcomp"] for s in steps), 2)
        diff = round(total_m100 - creditos_descontados_apuracao, 2)

        return {
            "tributo": tributo,
            "contrib_periodo": round(contrib_periodo, 2),
            "creditos_descontados_apuracao": round(creditos_descontados_apuracao, 2),
            "total_creditos_100": total_100,
            "total_creditos_200": total_200,
            "total_creditos": total_m100,
            "diff_m100_vs_apuracao": diff,
            "contrib_apos_100": round(max(0.0, contrib_periodo - total_100), 2),
            "contrib_restante": round(max(0.0, contrib_periodo - total_100 - total_200), 2),
            "total_perdcomp_disponivel": total_perdcomp,
            "steps": steps,
        }

    return {
        "pis": _build(dfs.get("M100"), dfs.get("M200"), "PIS"),
        "cofins": _build(dfs.get("M500"), dfs.get("M600"), "COFINS"),
    }
