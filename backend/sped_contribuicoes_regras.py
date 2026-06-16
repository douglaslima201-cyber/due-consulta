"""
Motor de regras para análise do SPED Contribuições (EFD-PIS/COFINS) de
transportadoras, com foco em:

  G1 - Créditos tomados nos blocos A, C e F (insumos típicos do setor de
       transporte de cargas)
  G2 - Ativo imobilizado / frota (registro F130/F120)
  G3 - Reconciliação entre os créditos apurados em A/C/F e os totais
       declarados no bloco M (M100/M500)
  G4 - Transposição de saldo dos registros 1100/1500 (multi-período)
  G5 - Rateio proporcional de créditos comuns (registro 0111)

Cada achado segue o formato:
  {
    "grupo": "G1".."G5",
    "bloco": "A"|"C"|"F"|"M"|"0"|"1",
    "registro": "A170"|"C170"|...,
    "competencia": "MM/AAAA",
    "tipo": "OPORTUNIDADE"|"RISCO"|"INCONSISTENCIA"|"INFORMATIVO",
    "descricao": str,
    "valor_envolvido": float,
    "base_legal": str,
    "recomendacao": str,
    "severidade": "BAIXO"|"MEDIO"|"ALTO",
  }

Todos os achados são pontos de atenção para revisão por especialista
tributário — não constituem posição definitiva sobre o aproveitamento de
créditos.
"""

from __future__ import annotations

import pandas as pd

from sped_contribuicoes_parser import (
    CST_COM_CREDITO, CST_RATEIO, CST_SEM_CREDITO, parse_decimal,
    build_item_map, build_conta_map, build_participante_map,
)

TOLERANCIA_VALOR = 1.0   # diferenças de até R$ 1,00 não são sinalizadas
TOLERANCIA_PCT = 0.01    # diferenças de até 1 ponto percentual no rateio

BASE_LEGAL_NAO_CUMULATIVO = "Lei 10.637/2002, art. 3º; Lei 10.833/2003, art. 3º; IN RFB 2.121/2022"
BASE_LEGAL_INSUMO = "Lei 10.637/2002, art. 3º, II; Lei 10.833/2003, art. 3º, II; STJ Tema 779 (REsp 1.221.170/PR); IN RFB 2.121/2022, art. 176"
BASE_LEGAL_ATIVO_IMOB = "Lei 10.637/2002, art. 3º, VI e § 14; Lei 10.833/2003, art. 3º, VI e § 14; IN RFB 2.121/2022, arts. 199 a 216"
BASE_LEGAL_RATEIO = "Lei 10.637/2002, art. 3º, §§ 8º e 9º; Lei 10.833/2003, art. 3º, §§ 8º e 9º; IN RFB 2.121/2022, Título V (apuração de créditos)"
BASE_LEGAL_FRETE_SUBCONTRATADO = "Lei 10.637/2002, art. 3º, IX; Lei 10.833/2003, art. 3º, IX; IN RFB 2.121/2022 (créditos sobre fretes)"
BASE_LEGAL_PEDAGIO = "Lei 10.209/2001 (Vale-Pedágio obrigatório — não integra o preço do frete); IN RFB 2.121/2022"


# ─── Categorias de insumo típicas de transportadoras ──────────────────────────
# categoria -> (lista de palavras-chave em CAIXA ALTA, descrição amigável)
CATEGORIAS_TRANSPORTADORA: dict[str, tuple[list[str], str]] = {
    "COMBUSTIVEL_LUBRIFICANTE": (
        ["DIESEL", "ETANOL", "GASOLINA", "ARLA", "COMBUSTIVEL", "COMBUSTÍVEL", "LUBRIFICANTE", "OLEO", "ÓLEO"],
        "Combustível / lubrificante",
    ),
    "PNEUS": (
        ["PNEU", "RECAPAGEM", "RECAUCHUTAGEM", "CAMARA DE AR", "CÂMARA DE AR"],
        "Pneus / recapagem",
    ),
    "PECAS_MANUTENCAO": (
        ["PECA", "PEÇA", "MANUTEN", "MECANIC", "MECÂNIC", "OFICINA", "CARROCERIA", "FUNILARIA", "REVISAO", "REVISÃO", "CONSERTO", "SERVIÇO TOMADO", "SERVICO TOMADO"],
        "Peças / manutenção / serviços mecânicos",
    ),
    "FRETE_SUBCONTRATADO": (
        ["SUB. CONT", "SUB CONT", "SUBCONTRAT", "FRETE", "AGREGADO", "TRANSPORTADOR AUTONOMO", "TRANSPORTADOR AUTÔNOMO"],
        "Frete subcontratado (PF/PJ)",
    ),
    "PEDAGIO": (
        ["PEDAGIO", "PEDÁGIO", "VALE-PEDAGIO", "VALE-PEDÁGIO"],
        "Pedágio",
    ),
    "SEGURO_CARGA": (
        ["SEGURO"],
        "Seguro de carga / RCTR-C / RC-DC",
    ),
    "RASTREAMENTO": (
        ["RASTRE", "MONITORAMENT", "TELEMETRIA"],
        "Rastreamento / monitoramento de cargas",
    ),
    "ARRENDAMENTO_LOCACAO_VEICULO": (
        ["ARRENDAMENTO", "LOCACAO", "LOCAÇÃO", "ALUGUEL", "LEASING"],
        "Arrendamento / locação de veículos",
    ),
    "ATIVO_IMOBILIZADO_FROTA": (
        ["VEICULO", "VEÍCULO", "FROTA", "CAMINHAO", "CAMINHÃO", "CAVALO MECANICO", "CAVALO MECÂNICO", "SEMIRREBOQUE", "CARRETA", "REBOQUE"],
        "Frota / ativo imobilizado",
    ),
}


def _classificar(*textos: str) -> tuple[str | None, str | None]:
    """Classifica um item/conta em uma categoria típica de transportadora,
    com base em palavras-chave. Retorna (categoria, descricao_categoria)."""
    alvo = " ".join(t.upper() for t in textos if t)
    for categoria, (kws, descricao) in CATEGORIAS_TRANSPORTADORA.items():
        if any(kw in alvo for kw in kws):
            return categoria, descricao
    return None, None


def _severidade_por_valor(valor: float) -> str:
    valor = abs(valor)
    if valor >= 10000:
        return "ALTO"
    if valor >= 1000:
        return "MEDIO"
    return "BAIXO"


def _achado(grupo, bloco, registro, competencia, tipo, descricao, valor, base_legal, recomendacao, severidade=None) -> dict:
    return {
        "grupo": grupo,
        "bloco": bloco,
        "registro": registro,
        "competencia": competencia,
        "tipo": tipo,
        "descricao": descricao,
        "valor_envolvido": round(float(valor), 2),
        "base_legal": base_legal,
        "recomendacao": recomendacao,
        "severidade": severidade or _severidade_por_valor(valor),
    }


# ─── G1 — Créditos tomados nos blocos A, C e F ────────────────────────────────

def _g1_creditos_acf(dfs: dict[str, pd.DataFrame], header: dict, item_map: dict, conta_map: dict) -> list[dict]:
    achados: list[dict] = []
    competencia = header.get("competencia", "")

    # A170 e C170: itens de mercadoria/serviço tomado
    for registro, bloco in (("A170", "A"), ("C170", "C")):
        df = dfs.get(registro)
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            cod_item = row.get("cod_item", "")
            descr_item = item_map.get(cod_item, "")
            cod_cta = row.get("cod_cta", "")
            nome_cta = conta_map.get(cod_cta, "")
            cst_pis = str(row.get("cst_pis", "")).strip()
            vl_pis = parse_decimal(row.get("vl_pis", "0"))
            vl_cofins = parse_decimal(row.get("vl_cofins", "0"))
            vl_item = parse_decimal(row.get("vl_item", "0"))
            categoria, descr_categoria = _classificar(cod_item, descr_item, nome_cta)

            tem_credito = cst_pis in CST_COM_CREDITO
            sem_credito = cst_pis in CST_SEM_CREDITO

            if categoria == "PEDAGIO" and tem_credito:
                achados.append(_achado(
                    "G1", bloco, registro, competencia, "RISCO",
                    f"Crédito de PIS/COFINS tomado sobre item classificado como pedágio "
                    f"('{descr_item or cod_item}', conta '{nome_cta or cod_cta}'), CST {cst_pis}.",
                    vl_pis + vl_cofins, BASE_LEGAL_PEDAGIO,
                    "O Vale-Pedágio obrigatório (Lei 10.209/2001) não integra o preço do frete "
                    "e não compõe base de crédito de PIS/COFINS. Solução: (1) Se o valor é "
                    "pedágio pago pelo embarcador (vale-pedágio), estornar o crédito via "
                    "ajuste de redução em M110/M510 (código RD_070 ou similar) e retificar "
                    "o SPED. (2) Se é despesa própria de pedágio/estacionamento da "
                    "transportadora, o item pode ser mantido como insumo — retificar o CST "
                    "para 50 e recalcular o crédito corretamente.",
                ))
                continue

            if categoria is not None and sem_credito:
                achados.append(_achado(
                    "G1", bloco, registro, competencia, "OPORTUNIDADE",
                    f"Item '{descr_item or cod_item}' (conta '{nome_cta or cod_cta}') "
                    f"classificado como {descr_categoria} foi lançado com CST {cst_pis} "
                    f"(sem direito a crédito), valor do item R$ {vl_item:,.2f}.",
                    vl_item * 0.0925, BASE_LEGAL_INSUMO,
                    f"Itens de {descr_categoria.lower()} costumam ser considerados insumo "
                    "essencial à atividade de transporte de cargas (Tema 779/STJ). "
                    "Verificar se o CST aplicado está correto e se há crédito de "
                    "PIS/COFINS não aproveitado sobre esta operação.",
                    severidade=_severidade_por_valor(vl_item * 0.0925),
                ))
                continue

            if categoria is None and tem_credito and (vl_pis > 0 or vl_cofins > 0):
                achados.append(_achado(
                    "G1", bloco, registro, competencia, "RISCO",
                    f"Crédito de PIS/COFINS (R$ {vl_pis + vl_cofins:,.2f}) tomado sobre item "
                    f"'{descr_item or cod_item}' (conta '{nome_cta or cod_cta}', CST {cst_pis}) "
                    "que não se encaixa nas categorias típicas de insumo de transportadoras.",
                    vl_pis + vl_cofins, BASE_LEGAL_INSUMO,
                    "Verificar o enquadramento como insumo (essencialidade à atividade de "
                    "transporte, conforme Tema 779/STJ). Solução: se não caracterizado como "
                    "insumo, estornar o crédito via ajuste de redução em M110/M510 "
                    "(código RD_070) com retificação do SPED. Se o item for confirmado "
                    "como insumo por parecer jurídico, documenter o enquadramento para "
                    "suportar eventual questionamento fiscal.",
                ))

    # F100: demais operações (frete subcontratado PF/PJ, arrendamento, etc.)
    df_f100 = dfs.get("F100")
    if df_f100 is not None and not df_f100.empty:
        for _, row in df_f100.iterrows():
            cod_item = row.get("cod_item", "")
            descr_item = item_map.get(cod_item, "") or row.get("desc_doc_oper", "")
            cod_cta = row.get("cod_cta", "")
            nome_cta = conta_map.get(cod_cta, "")
            cst_pis = str(row.get("cst_pis", "")).strip()
            vl_oper = parse_decimal(row.get("vl_oper", "0"))
            vl_bc_pis = parse_decimal(row.get("vl_bc_pis", "0"))
            aliq_pis = parse_decimal(row.get("aliq_pis", "0"))
            vl_pis = parse_decimal(row.get("vl_pis", "0"))
            vl_bc_cofins = parse_decimal(row.get("vl_bc_cofins", "0"))
            aliq_cofins = parse_decimal(row.get("aliq_cofins", "0"))
            vl_cofins = parse_decimal(row.get("vl_cofins", "0"))

            categoria, descr_categoria = _classificar(cod_item, descr_item, nome_cta)

            if categoria == "FRETE_SUBCONTRATADO" and vl_oper > 0:
                pct_base = (vl_bc_pis / vl_oper) if vl_oper else 0.0
                if pct_base < 0.999:
                    aliq_padrao = abs(aliq_pis - 1.65) > 0.01 or abs(aliq_cofins - 7.6) > 0.01
                    achados.append(_achado(
                        "G1", "F", "F100", competencia, "INCONSISTENCIA",
                        f"F100 '{descr_item or cod_item}' (fornecedor "
                        f"{row.get('cod_part','')}): valor da operação R$ {vl_oper:,.2f}, "
                        f"mas apenas R$ {vl_bc_pis:,.2f} ({pct_base * 100:.1f}%) compõe a "
                        f"base de cálculo de crédito de PIS/COFINS"
                        + (f", com alíquotas reduzidas (PIS {aliq_pis}% / COFINS {aliq_cofins}%)" if aliq_padrao else "")
                        + ".",
                        vl_oper - vl_bc_pis, BASE_LEGAL_FRETE_SUBCONTRATADO,
                        "Solução: (1) Se a base reduzida é justificada por repasse de "
                        "vale-pedágio ao subcontratado, documentar o valor excluído e "
                        "manter o lançamento atual. (2) Caso contrário, retificar o "
                        "registro F100 ajustando vl_bc_pis/vl_bc_cofins para o valor "
                        "total do frete contratado. Recalcular PIS/COFINS e refletir nos "
                        "registros M100/M500 via acréscimo M110/M510 ou retificação do SPED.",
                        severidade=_severidade_por_valor(vl_oper - vl_bc_pis),
                    ))
                continue

            if categoria is not None and cst_pis in CST_SEM_CREDITO and vl_oper > 0:
                achados.append(_achado(
                    "G1", "F", "F100", competencia, "OPORTUNIDADE",
                    f"F100 '{descr_item or cod_item}' (conta '{nome_cta or cod_cta}', "
                    f"categoria {descr_categoria}) lançado com CST {cst_pis} (sem "
                    f"crédito), valor da operação R$ {vl_oper:,.2f}.",
                    vl_oper * 0.0925, BASE_LEGAL_INSUMO,
                    f"Verificar se esta operação de {descr_categoria.lower()} dá direito "
                    "a crédito de PIS/COFINS e se o CST aplicado está correto.",
                    severidade=_severidade_por_valor(vl_oper * 0.0925),
                ))

    return achados


# ─── G2 — Ativo imobilizado / frota (F120/F130) ───────────────────────────────

def _g2_ativo_imobilizado(dfs: dict[str, pd.DataFrame], header: dict, conta_map: dict) -> list[dict]:
    achados: list[dict] = []
    competencia = header.get("competencia", "")

    df = dfs.get("F130")
    if df is None or df.empty:
        return achados

    for _, row in df.iterrows():
        cod_cta = row.get("cod_cta", "")
        nome_cta = conta_map.get(cod_cta, "")
        ident_bem = str(row.get("ident_bem_imob", "")).strip()
        vl_oper_aquis = parse_decimal(row.get("vl_oper_aquis", "0"))
        vl_bc_cred = parse_decimal(row.get("vl_bc_cred", "0"))
        vl_bc_pis = parse_decimal(row.get("vl_bc_pis", "0"))
        cst_pis = str(row.get("cst_pis", "")).strip()
        mes_oper_aquis = row.get("mes_oper_aquis", "")

        categoria, _ = _classificar(nome_cta, ident_bem)
        eh_frota = categoria == "ATIVO_IMOBILIZADO_FROTA" or ident_bem == "06"

        # Verifica se a parcela mensal corresponde a 1/48 do valor de aquisição
        # (regra geral de apropriação de crédito sobre ativo imobilizado).
        if vl_bc_cred > 0:
            parcela_esperada = vl_bc_cred / 48
            if abs(parcela_esperada - vl_bc_pis) > TOLERANCIA_VALOR:
                achados.append(_achado(
                    "G2", "F", "F130", competencia, "INCONSISTENCIA",
                    f"F130 ({nome_cta or cod_cta}, aquisição {mes_oper_aquis}): base de "
                    f"cálculo do crédito informada na parcela (R$ {vl_bc_pis:,.2f}) "
                    f"diverge do valor esperado para o método de 1/48 sobre "
                    f"R$ {vl_bc_cred:,.2f} (R$ {parcela_esperada:,.2f}).",
                    vl_bc_pis - parcela_esperada, BASE_LEGAL_ATIVO_IMOB,
                    "Solução: (1) Verificar o valor de aquisição do bem e o método de "
                    "apropriação (1/48 ou prazo diferente por normativa específica). "
                    "(2) Retificar o registro F130 ajustando vl_bc_pis/vl_bc_cofins para "
                    "o valor correto da parcela mensal. (3) Refletir a correção em M100/"
                    "M500 — se o crédito foi sub-aproveitado, incluir acréscimo em M110/"
                    "M510; se foi super-aproveitado, incluir redução.",
                ))

        if eh_frota and cst_pis in CST_RATEIO:
            achados.append(_achado(
                "G2", "F", "F130", competencia, "INFORMATIVO",
                f"Crédito sobre veículo da frota ({nome_cta or cod_cta}, aquisição "
                f"{mes_oper_aquis}, valor de aquisição R$ {vl_oper_aquis:,.2f}) está "
                f"classificado com CST {cst_pis}, sujeito ao rateio proporcional do "
                "registro 0111 (ver G5).",
                vl_bc_pis, BASE_LEGAL_RATEIO,
                "Confirmar que o percentual de rateio aplicado a este crédito está "
                "consistente com a receita bruta declarada em 0111 (ver achados do "
                "grupo G5 — Rateio proporcional).",
                severidade="BAIXO",
            ))
        elif eh_frota and cst_pis in CST_COM_CREDITO:
            achados.append(_achado(
                "G2", "F", "F130", competencia, "INFORMATIVO",
                f"Crédito sobre veículo da frota ({nome_cta or cod_cta}, aquisição "
                f"{mes_oper_aquis}, valor de aquisição R$ {vl_oper_aquis:,.2f}, parcela "
                f"do mês R$ {vl_bc_pis:,.2f}, CST {cst_pis}).",
                vl_bc_pis, BASE_LEGAL_ATIVO_IMOB,
                "Crédito de ativo imobilizado (frota) identificado e classificado com "
                "CST de crédito integral (sem rateio).",
                severidade="BAIXO",
            ))

    return achados


# ─── G3 — Reconciliação Bloco M ───────────────────────────────────────────────

def _soma_creditos_origem(dfs: dict[str, pd.DataFrame], campo_vl: str, campo_cst: str) -> float:
    """Soma os valores de crédito (PIS ou COFINS) declarados em A170/C170/F100,
    considerando apenas linhas com CST de crédito (CST 50-55, 60-65 — crédito
    integral, sem rateio; CST 56/66 são tratados separadamente em G5)."""
    total = 0.0
    for registro in ("A170", "C170", "F100"):
        df = dfs.get(registro)
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            cst = str(row.get(campo_cst, "")).strip()
            if cst in CST_COM_CREDITO and cst not in CST_RATEIO:
                total += parse_decimal(row.get(campo_vl, "0"))
    return total


def _soma_creditos_rateados_f130(dfs: dict[str, pd.DataFrame], campo_m105: str, campo_vl_m105: str) -> float:
    """Soma, a partir do M105/M505, a parcela já rateada (vl_bc_pis/vl_bc_cofins
    pós-rateio) dos créditos com CST 56/66 (tipicamente originados do F130/F120,
    sujeitos ao rateio proporcional do 0111), multiplicada pela alíquota."""
    df = dfs.get(campo_m105)
    if df is None or df.empty:
        return 0.0
    total = 0.0
    aliquota = 0.0165 if campo_m105 == "M105" else 0.076
    cst_col = "cst_pis" if campo_m105 == "M105" else "cst_cofins"
    for _, row in df.iterrows():
        if str(row.get(cst_col, "")).strip() in CST_RATEIO:
            total += parse_decimal(row.get(campo_vl_m105, "0")) * aliquota
    return total


def _g3_reconciliacao_m(dfs: dict[str, pd.DataFrame], header: dict) -> list[dict]:
    achados: list[dict] = []
    competencia = header.get("competencia", "")

    for tributo, registro_m, campo_vl, campo_cst, registro_m105, campo_vl_m105 in (
        ("PIS", "M100", "vl_pis", "cst_pis", "M105", "vl_bc_pis"),
        ("COFINS", "M500", "vl_cofins", "cst_cofins", "M505", "vl_bc_cofins"),
    ):
        df_m = dfs.get(registro_m)
        if df_m is None or df_m.empty:
            continue

        total_acf = _soma_creditos_origem(dfs, campo_vl, campo_cst)
        total_f130_rateado = _soma_creditos_rateados_f130(dfs, registro_m105, campo_vl_m105)
        total_declarado = df_m["vl_cred_apur"].apply(parse_decimal).sum()

        total_esperado = total_acf + total_f130_rateado
        diferenca = total_declarado - total_esperado

        if abs(diferenca) > TOLERANCIA_VALOR:
            achados.append(_achado(
                "G3", "M", registro_m, competencia, "INCONSISTENCIA",
                f"Soma dos créditos de {tributo} apurados diretamente nos blocos "
                f"A/C/F (R$ {total_esperado:,.2f}, incluindo a parcela rateada do "
                f"ativo imobilizado) não corresponde ao total declarado em "
                f"{registro_m} (R$ {total_declarado:,.2f}). Diferença: "
                f"R$ {diferenca:,.2f}.",
                diferenca, BASE_LEGAL_NAO_CUMULATIVO,
                f"Solução: (1) Verificar os registros M110/M510 (ajustes de acréscimo/"
                f"redução) para identificar a origem da diferença. (2) Se a diferença "
                f"é favorável ao contribuinte (sub-aproveitamento), apropriar o saldo "
                f"via crédito extemporâneo no M100/M500 da competência atual com ajuste "
                f"M110 (código AC_XXX). (3) Se desfavorável (excesso), retificar o SPED "
                f"reduzindo o crédito declarado em {registro_m} e providenciar o "
                f"recolhimento da diferença com SELIC, se aplicável.",
            ))
        else:
            achados.append(_achado(
                "G3", "M", registro_m, competencia, "INFORMATIVO",
                f"Soma dos créditos de {tributo} apurados em A/C/F "
                f"(R$ {total_esperado:,.2f}) é consistente com o total declarado em "
                f"{registro_m} (R$ {total_declarado:,.2f}).",
                total_declarado, BASE_LEGAL_NAO_CUMULATIVO,
                "Nenhuma ação necessária — valores reconciliados.",
                severidade="BAIXO",
            ))

    return achados


# ─── G4 — Transposição de saldo 1100/1500 (multi-período) ────────────────────

def _resumo_1100_1500(dfs: dict[str, pd.DataFrame], registro: str, competencia: str) -> list[dict]:
    """Achados informativos (arquivo único) listando os créditos de ativo
    imobilizado em controle nos registros 1100 (PIS) / 1500 (COFINS)."""
    achados: list[dict] = []
    df = dfs.get(registro)
    if df is None or df.empty:
        return achados

    bloco = "1"
    total = df["vl_cred"].apply(parse_decimal).sum()
    achados.append(_achado(
        "G4", bloco, registro, competencia, "INFORMATIVO",
        f"Registro {registro}: {len(df)} período(s) de origem com crédito de "
        f"ativo imobilizado em controle, totalizando R$ {total:,.2f} no período.",
        total, BASE_LEGAL_ATIVO_IMOB,
        "Envie também o(s) arquivo(s) SPED do(s) mês(es) seguinte(s) para que o "
        "sistema verifique a transposição de saldo (continuidade dos créditos "
        "de ativo imobilizado de um período para o outro).",
        severidade="BAIXO",
    ))
    return achados


def gerar_achados_transposicao(periodos: list[tuple[dict, dict[str, pd.DataFrame]]]) -> list[dict]:
    """Recebe uma lista de (header, dfs) ordenada por competência e gera
    achados de transposição de saldo entre registros 1100 (PIS) e 1500
    (COFINS) de períodos consecutivos."""
    achados: list[dict] = []
    if len(periodos) < 2:
        return achados

    for registro, bloco, base_legal in (("1100", "1", BASE_LEGAL_ATIVO_IMOB), ("1500", "1", BASE_LEGAL_ATIVO_IMOB)):
        for i in range(len(periodos) - 1):
            header_a, dfs_a = periodos[i]
            header_b, dfs_b = periodos[i + 1]
            comp_a = header_a.get("competencia", "")
            comp_b = header_b.get("competencia", "")

            df_a = dfs_a.get(registro)
            df_b = dfs_b.get(registro)
            if df_a is None or df_a.empty:
                continue

            mapa_b: dict[tuple[str, str], float] = {}
            if df_b is not None and not df_b.empty:
                for _, row in df_b.iterrows():
                    chave = (str(row.get("per_apur_cred", "")), str(row.get("cod_cred", "")))
                    mapa_b[chave] = mapa_b.get(chave, 0.0) + parse_decimal(row.get("vl_cred", "0"))

            for _, row in df_a.iterrows():
                per_apur = str(row.get("per_apur_cred", ""))
                cod_cred = str(row.get("cod_cred", ""))
                vl_a = parse_decimal(row.get("vl_cred", "0"))
                if vl_a <= 0:
                    continue
                chave = (per_apur, cod_cred)

                if chave not in mapa_b:
                    achados.append(_achado(
                        "G4", bloco, registro, comp_b, "RISCO",
                        f"Crédito de ativo imobilizado de origem {per_apur} (código "
                        f"{cod_cred}, R$ {vl_a:,.2f} em {comp_a}) não aparece mais no "
                        f"{registro} de {comp_b}.",
                        vl_a, base_legal,
                        "Solução: (1) Se houve alienação/baixa do bem, documentar com "
                        "nota fiscal de saída e registrar ajuste de encerramento do "
                        "crédito no M100/M500 (código de redução RD_XXX). (2) Se não "
                        "houve alienação, o crédito foi omitido indevidamente: retificar "
                        "o SPED incluindo a linha do período de origem no registro "
                        "1100/1500 e apropriar os créditos não aproveitados via acréscimo "
                        "em M110/M510 (crédito extemporâneo, sujeito a SELIC em favor do contribuinte).",
                    ))
                else:
                    vl_b = mapa_b[chave]
                    if abs(vl_a - vl_b) > TOLERANCIA_VALOR:
                        achados.append(_achado(
                            "G4", bloco, registro, comp_b, "INCONSISTENCIA",
                            f"Crédito de ativo imobilizado de origem {per_apur} (código "
                            f"{cod_cred}) variou de R$ {vl_a:,.2f} em {comp_a} para "
                            f"R$ {vl_b:,.2f} em {comp_b} sem justificativa aparente.",
                            vl_b - vl_a, base_legal,
                            "Solução: (1) Identificar a competência onde a variação foi "
                            "originada (reajuste, baixa parcial ou erro de digitação). "
                            "(2) Retificar o SPED daquela competência corrigindo o saldo "
                            "no registro 1100/1500. (3) Propagar a correção para as "
                            "competências subsequentes, ajustando M100/M500 via acréscimo "
                            "ou redução conforme a diferença apurada.",
                        ))

    return achados


# ─── G5 — Rateio proporcional (registro 0111) ─────────────────────────────────

# Para cada CST de vinculação mista, percentual do total que compõe a base
# de crédito do regime não-cumulativo: soma das proporções de receita
# tributada no mercado interno e de exportação dentro do(s) regime(s) a que
# o crédito está vinculado.
_CST_RATEIO_PCT_FUNC = {
    "53": lambda rb: (rb["pct_trib_mi"] / (rb["pct_trib_mi"] + rb["pct_nt_mi"])) if (rb["pct_trib_mi"] + rb["pct_nt_mi"]) else 0.0,
    "54": lambda rb: (rb["pct_trib_mi"] / (rb["pct_trib_mi"] + rb["pct_exp"])) if (rb["pct_trib_mi"] + rb["pct_exp"]) else 0.0,
    "55": lambda rb: (rb["pct_exp"] / (rb["pct_nt_mi"] + rb["pct_exp"])) if (rb["pct_nt_mi"] + rb["pct_exp"]) else 0.0,
    "56": lambda rb: rb["pct_trib_mi"] + rb["pct_exp"],
}


def _g5_rateio(dfs: dict[str, pd.DataFrame], header: dict) -> list[dict]:
    achados: list[dict] = []
    competencia = header.get("competencia", "")
    receita_bruta = header.get("receita_bruta")

    if not receita_bruta or receita_bruta["total"] <= 0:
        return achados

    rateio_indicado = header.get("rateio_proporcional", False)
    multiplas_categorias = sum(1 for k in ("pct_trib_mi", "pct_nt_mi", "pct_exp") if receita_bruta[k] > 0) > 1

    achados.append(_achado(
        "G5", "0", "0111", competencia, "INFORMATIVO",
        f"Receita bruta do período (registro 0111): tributada no mercado interno "
        f"{receita_bruta['pct_trib_mi'] * 100:.2f}% (R$ {receita_bruta['trib_mi']:,.2f}), "
        f"não tributada {receita_bruta['pct_nt_mi'] * 100:.2f}% "
        f"(R$ {receita_bruta['nt_mi']:,.2f}), exportação "
        f"{receita_bruta['pct_exp'] * 100:.2f}% (R$ {receita_bruta['exp']:,.2f}), "
        f"cumulativo {receita_bruta['pct_cum'] * 100:.2f}% (R$ {receita_bruta['cum']:,.2f}).",
        receita_bruta["total"], BASE_LEGAL_RATEIO,
        "Percentuais de referência para o rateio proporcional dos créditos comuns "
        "(CST 53 a 56 e 63 a 66) declarados em M105/M505.",
        severidade="BAIXO",
    ))

    if multiplas_categorias and not rateio_indicado:
        achados.append(_achado(
            "G5", "0", "0110", competencia, "INCONSISTENCIA",
            "O registro 0110 indica método de apropriação direta de créditos "
            "(ind_apro_cred = 1), mas a empresa possui receita em mais de uma "
            "categoria (tributada / não tributada / exportação) no registro 0111.",
            receita_bruta["total"], BASE_LEGAL_RATEIO,
            "Solução: (1) Avaliar com a área tributária se o método mais adequado "
            "é o proporcional (0110 com ind_apro_cred = 2 ou 3). (2) Se confirmado, "
            "retificar o registro 0110 e recalcular os créditos comuns em M105/M505 "
            "com os percentuais corretos do 0111. (3) As competências em aberto devem "
            "ser retificadas; créditos sub-aproveitados podem ser recuperados via "
            "crédito extemporâneo no mês atual.",
        ))

    if not rateio_indicado:
        return achados

    for registro_m105, label, cst_col, base_col in (
        ("M105", "PIS", "cst_pis", "vl_bc_pis"),
        ("M505", "COFINS", "cst_cofins", "vl_bc_cofins"),
    ):
        df = dfs.get(registro_m105)
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            cst = str(row.get(cst_col, "")).strip()
            pct_func = _CST_RATEIO_PCT_FUNC.get(cst)
            if pct_func is None:
                continue

            vl_bc_total = parse_decimal(row.get(f"{base_col}_tot", "0"))
            vl_bc_declarado = parse_decimal(row.get(base_col, "0"))
            pct_esperado = pct_func(receita_bruta)
            vl_bc_esperado = vl_bc_total * pct_esperado

            if abs(vl_bc_declarado - vl_bc_esperado) > max(TOLERANCIA_VALOR, vl_bc_total * TOLERANCIA_PCT):
                achados.append(_achado(
                    "G5", "M", registro_m105, competencia, "INCONSISTENCIA",
                    f"{registro_m105} (NAT_BC_CRED {row.get('nat_bc_cred','')}, CST {cst}): "
                    f"base de cálculo total R$ {vl_bc_total:,.2f}, rateio esperado "
                    f"{pct_esperado * 100:.2f}% (R$ {vl_bc_esperado:,.2f}), mas o valor "
                    f"declarado após rateio foi R$ {vl_bc_declarado:,.2f}.",
                    vl_bc_declarado - vl_bc_esperado, BASE_LEGAL_RATEIO,
                    f"Solução: (1) Recalcular o campo PERC_RAT_CRED do registro "
                    f"{registro_m105} usando os percentuais exatos do 0111 (receita "
                    f"tributada MI + exportação ÷ receita total). (2) Retificar o SPED "
                    f"ajustando o campo {base_col} com o valor correto após rateio. "
                    f"(3) Se houve sub-aproveitamento de {label}, a diferença pode ser "
                    f"recuperada como crédito extemporâneo via M110/M510 (acréscimo) "
                    f"na competência atual, com incidência de SELIC em favor do contribuinte. "
                    f"(4) Se houve aproveitamento a maior, recolher a diferença com DARF "
                    f"e SELIC antes de notificação fiscal.",
                ))

    return achados


# ─── Função principal ─────────────────────────────────────────────────────────

def gerar_achados(dfs: dict[str, pd.DataFrame], header: dict) -> list[dict]:
    """Gera todos os achados (G1-G3 e G5) para um único período/arquivo SPED.
    G4 (transposição multi-período) é gerado separadamente, via
    ``gerar_achados_transposicao``, quando há 2+ arquivos."""
    item_map = build_item_map(dfs)
    conta_map = build_conta_map(dfs)
    build_participante_map(dfs)  # reservado para enriquecimento futuro

    achados: list[dict] = []
    achados += _g1_creditos_acf(dfs, header, item_map, conta_map)
    achados += _g2_ativo_imobilizado(dfs, header, conta_map)
    achados += _g3_reconciliacao_m(dfs, header)
    achados += _resumo_1100_1500(dfs, "1100", header.get("competencia", ""))
    achados += _resumo_1100_1500(dfs, "1500", header.get("competencia", ""))
    achados += _g5_rateio(dfs, header)
    return achados
