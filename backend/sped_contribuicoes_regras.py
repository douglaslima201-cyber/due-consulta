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
    build_participante_tipo_map,
)

TOLERANCIA_VALOR = 1.0   # diferenças de até R$ 1,00 não são sinalizadas
TOLERANCIA_PCT = 0.01    # diferenças de até 1 ponto percentual no rateio

BASE_LEGAL_NAO_CUMULATIVO = "Lei 10.637/2002, art. 3º; Lei 10.833/2003, art. 3º; IN RFB 2.121/2022"
BASE_LEGAL_INSUMO = "Lei 10.637/2002, art. 3º, II; Lei 10.833/2003, art. 3º, II; STJ Tema 779 (REsp 1.221.170/PR); IN RFB 2.121/2022, art. 176"
BASE_LEGAL_ATIVO_IMOB = "Lei 10.637/2002, art. 3º, VI e § 14; Lei 10.833/2003, art. 3º, VI e § 14; IN RFB 2.121/2022, arts. 199 a 216"
BASE_LEGAL_CONTROLE_CREDITO = "Lei 10.637/2002, art. 3º e seguintes; Lei 10.833/2003, art. 3º e seguintes; IN RFB 2.121/2022 — Registros de controle de créditos 1100/1500 (Guia Prático EFD-Contribuições)"
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

BASE_LEGAL_SUBCONTRATADO_PF = (
    "Lei 10.637/2002, art. 3º, §4º; Lei 10.833/2003, art. 3º, §4º; "
    "IN RFB 2.121/2022 — crédito sobre frete subcontratado de PF ou PJ Simples = 75% das alíquotas normais"
)

def _g1_creditos_acf(dfs: dict[str, pd.DataFrame], header: dict, item_map: dict, conta_map: dict,
                     participante_tipo_map: dict | None = None) -> list[dict]:
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
                cod_part = str(row.get("cod_part", "")).strip()
                tipo_part = (participante_tipo_map or {}).get(cod_part, "PJ")
                descr_upper = (descr_item or cod_item or "").upper()
                is_simples = tipo_part == "PJ" and any(
                    kw in descr_upper for kw in ("SIMPLES", "MEI", "MICRO", "EPP")
                )
                is_pf = tipo_part == "PF"
                tipo_contratado = (
                    "Pessoa Física (autônomo)" if is_pf else
                    "PJ optante pelo Simples Nacional" if is_simples else None
                )

                if tipo_contratado:
                    # Para PF e Simples, alíquotas corretas = 75% das normais:
                    # PIS 1,2375% (75% de 1,65%) e COFINS 5,70% (75% de 7,60%)
                    aliq_pis_correta = round(1.65 * 0.75, 4)   # 1.2375
                    aliq_cofins_correta = round(7.60 * 0.75, 2) # 5.70
                    tomou_integral = aliq_pis > aliq_pis_correta + 0.05
                    nao_tomou = aliq_pis < 0.01
                    if tomou_integral:
                        achados.append(_achado(
                            "G1", "F", "F100", competencia, "RISCO",
                            f"F100 frete subcontratado de {tipo_contratado} "
                            f"(fornecedor {cod_part}, R$ {vl_oper:,.2f}): crédito calculado "
                            f"às alíquotas integrais (PIS {aliq_pis}% / COFINS {aliq_cofins}%). "
                            f"Para {tipo_contratado} o crédito corresponde a 75% das "
                            f"alíquotas normais (PIS {aliq_pis_correta}% / COFINS {aliq_cofins_correta}%).",
                            vl_pis + vl_cofins, BASE_LEGAL_SUBCONTRATADO_PF,
                            f"Solução: Recalcular o crédito usando PIS {aliq_pis_correta}% e "
                            f"COFINS {aliq_cofins_correta}% sobre a base de R$ {vl_bc_pis:,.2f}. "
                            "Retificar F100 e ajustar M100/M500 via redução em M110/M510 "
                            "(estornar a diferença entre o crédito tomado e o crédito correto a 75%).",
                            severidade=_severidade_por_valor((aliq_pis - aliq_pis_correta) / 100 * vl_bc_pis),
                        ))
                    elif nao_tomou:
                        achados.append(_achado(
                            "G1", "F", "F100", competencia, "OPORTUNIDADE",
                            f"F100 frete subcontratado de {tipo_contratado} "
                            f"(fornecedor {cod_part}, R$ {vl_oper:,.2f}): nenhum crédito tomado "
                            f"(alíquota PIS = 0%). Para {tipo_contratado} é possível apropriar "
                            f"crédito a 75% das alíquotas normais (PIS {aliq_pis_correta}% / "
                            f"COFINS {aliq_cofins_correta}%).",
                            vl_bc_pis * aliq_pis_correta / 100 + vl_bc_cofins * aliq_cofins_correta / 100,
                            BASE_LEGAL_SUBCONTRATADO_PF,
                            f"Apropriar crédito em F100 com PIS {aliq_pis_correta}% e COFINS "
                            f"{aliq_cofins_correta}% sobre a base. Refletir em M100/M500 via "
                            "acréscimo em M110/M510 ou retificação do SPED.",
                            severidade=_severidade_por_valor(
                                vl_bc_pis * aliq_pis_correta / 100 + vl_bc_cofins * aliq_cofins_correta / 100
                            ),
                        ))

                # Verificação da base de cálculo (independente de PF/Simples)
                pct_base = (vl_bc_pis / vl_oper) if vl_oper else 0.0
                if pct_base < 0.999:
                    aliq_padrao = abs(aliq_pis - 1.65) > 0.01 or abs(aliq_cofins - 7.6) > 0.01
                    achados.append(_achado(
                        "G1", "F", "F100", competencia, "INCONSISTENCIA",
                        f"F100 '{descr_item or cod_item}' (fornecedor "
                        f"{cod_part}): valor da operação R$ {vl_oper:,.2f}, "
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

    # ── F130 — crédito sobre valor de aquisição ───────────────────────────────
    df_f130 = dfs.get("F130")
    if df_f130 is not None and not df_f130.empty:
        for _, row in df_f130.iterrows():
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

            if eh_frota:
                # Transportadoras devem apropriar crédito de frota EXCLUSIVAMENTE
                # pela depreciação (F120), não pelo valor de aquisição (F130).
                achados.append(_achado(
                    "G2", "F", "F130", competencia, "RISCO",
                    f"F130 ({nome_cta or cod_cta}, aquisição {mes_oper_aquis}, "
                    f"valor R$ {vl_oper_aquis:,.2f}): crédito de PIS/COFINS sobre "
                    "veículo da frota tomado pelo método de valor de aquisição. "
                    "Transportadoras devem apropriar o crédito exclusivamente pela "
                    "parcela de depreciação (registro F120), não pela aquisição.",
                    vl_bc_pis, BASE_LEGAL_ATIVO_IMOB,
                    "Solução: (1) Estornar o crédito tomado via F130 para a frota "
                    "(redução em M110/M510). (2) Adotar o registro F120 (encargos de "
                    "depreciação): o crédito mensal corresponde ao valor da depreciação "
                    "contábil do bem, calculado sobre o valor de aquisição e a vida útil "
                    "adotada. (3) Retificar o SPED substituindo os registros F130 por "
                    "F120 para os veículos da frota e ajustando M100/M500.",
                    severidade=_severidade_por_valor(vl_bc_pis),
                ))
                # Mesmo sendo método incorreto, valida o cálculo da parcela
                if vl_bc_cred > 0:
                    parcela_esperada = vl_bc_cred / 48
                    if abs(parcela_esperada - vl_bc_pis) > TOLERANCIA_VALOR:
                        achados.append(_achado(
                            "G2", "F", "F130", competencia, "INCONSISTENCIA",
                            f"F130 frota ({nome_cta or cod_cta}, aquisição {mes_oper_aquis}): "
                            f"além do método incorreto, a parcela mensal (R$ {vl_bc_pis:,.2f}) "
                            f"diverge do esperado para 1/48 sobre R$ {vl_bc_cred:,.2f} "
                            f"(esperado R$ {parcela_esperada:,.2f}).",
                            vl_bc_pis - parcela_esperada, BASE_LEGAL_ATIVO_IMOB,
                            "Solução: além de migrar para o método F120 (depreciação), "
                            "verificar o cálculo da parcela. No método F130 a parcela "
                            "mensal deve ser 1/48 do valor de aquisição — diferença de "
                            f"R$ {abs(vl_bc_pis - parcela_esperada):,.2f} deve ser "
                            "corrigida antes da retificação do SPED.",
                        ))
            else:
                # Para outros bens do imobilizado (não frota), verifica 1/48
                if vl_bc_cred > 0:
                    parcela_esperada = vl_bc_cred / 48
                    if abs(parcela_esperada - vl_bc_pis) > TOLERANCIA_VALOR:
                        achados.append(_achado(
                            "G2", "F", "F130", competencia, "INCONSISTENCIA",
                            f"F130 ({nome_cta or cod_cta}, aquisição {mes_oper_aquis}): parcela "
                            f"do mês (R$ {vl_bc_pis:,.2f}) diverge do esperado para 1/48 sobre "
                            f"R$ {vl_bc_cred:,.2f} (esperado R$ {parcela_esperada:,.2f}).",
                            vl_bc_pis - parcela_esperada, BASE_LEGAL_ATIVO_IMOB,
                            "Solução: (1) Verificar o valor de aquisição e o método de "
                            "apropriação (1/48 ou prazo diferente). (2) Retificar F130 "
                            "ajustando vl_bc_pis/vl_bc_cofins. (3) Refletir em M100/M500 "
                            "via M110/M510 — sub-aproveitamento: acréscimo; super-aproveitamento: redução.",
                        ))
                if cst_pis in CST_RATEIO:
                    achados.append(_achado(
                        "G2", "F", "F130", competencia, "INFORMATIVO",
                        f"F130 ({nome_cta or cod_cta}, aquisição {mes_oper_aquis}, "
                        f"R$ {vl_oper_aquis:,.2f}): CST {cst_pis}, sujeito ao rateio (0111).",
                        vl_bc_pis, BASE_LEGAL_RATEIO,
                        "Confirmar que o percentual de rateio do 0111 foi aplicado "
                        "corretamente neste crédito (ver achados G5).",
                        severidade="BAIXO",
                    ))

    # ── F120 — crédito sobre encargos de depreciação (método correto para frota) ─
    df_f120 = dfs.get("F120")
    if df_f120 is not None and not df_f120.empty:
        for _, row in df_f120.iterrows():
            cod_cta = row.get("cod_cta", "")
            nome_cta = conta_map.get(cod_cta, "")
            ident_bem = str(row.get("ident_bem_imob", "")).strip()
            mes_oper_aquis = row.get("mes_oper_aquis", "")
            vl_oper_depre = parse_decimal(row.get("vl_oper_depre", "0"))
            vl_bc_cred = parse_decimal(row.get("vl_bc_cred", "0"))
            vl_bc_pis = parse_decimal(row.get("vl_bc_pis", "0"))
            cst_pis = str(row.get("cst_pis", "")).strip()

            categoria, _ = _classificar(nome_cta, ident_bem)
            eh_frota = categoria == "ATIVO_IMOBILIZADO_FROTA" or ident_bem == "06"

            if eh_frota:
                achados.append(_achado(
                    "G2", "F", "F120", competencia, "INFORMATIVO",
                    f"F120 ({nome_cta or cod_cta}, aquisição {mes_oper_aquis}): crédito "
                    f"sobre encargos de depreciação de frota (R$ {vl_bc_pis:,.2f}) — "
                    "método correto para transportadoras.",
                    vl_bc_pis, BASE_LEGAL_ATIVO_IMOB,
                    "Método de depreciação (F120) é o correto para frota. "
                    "Confirmar que o encargo de depreciação contábil e o CST "
                    f"({cst_pis}) estão corretos e que o crédito foi refletido em M100/M500.",
                    severidade="BAIXO",
                ))
            else:
                if vl_bc_cred > 0 and abs(vl_bc_pis - vl_oper_depre) > TOLERANCIA_VALOR and vl_oper_depre > 0:
                    achados.append(_achado(
                        "G2", "F", "F120", competencia, "INCONSISTENCIA",
                        f"F120 ({nome_cta or cod_cta}, aquisição {mes_oper_aquis}): base do "
                        f"crédito (R$ {vl_bc_pis:,.2f}) diverge do encargo de depreciação "
                        f"declarado (R$ {vl_oper_depre:,.2f}).",
                        vl_bc_pis - vl_oper_depre, BASE_LEGAL_ATIVO_IMOB,
                        "Verificar o valor do encargo de depreciação contábil e ajustar "
                        "vl_bc_pis/vl_bc_cofins no F120 para que correspondam ao encargo real.",
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
    """Achados informativos (arquivo único) listando os créditos em controle
    nos registros 1100 (PIS) / 1500 (COFINS) — controle de saldo de crédito."""
    achados: list[dict] = []
    df = dfs.get(registro)
    if df is None or df.empty:
        return achados

    bloco = "1"
    tributo = "PIS" if registro == "1100" else "COFINS"
    total_cred = df["vl_cred"].apply(parse_decimal).sum()
    total_desc_ant = df["vl_cred_desc_pa_ant"].apply(parse_decimal).sum() if "vl_cred_desc_pa_ant" in df.columns else 0.0
    total_desc_pa = df["vl_cred_desc_pa"].apply(parse_decimal).sum() if "vl_cred_desc_pa" in df.columns else 0.0
    total_sld = df["sld_cred_final"].apply(parse_decimal).sum() if "sld_cred_final" in df.columns else 0.0

    descricao = (
        f"Registro {registro} ({tributo}): {len(df)} linha(s) de controle de crédito. "
        f"Fórmula: Saldo inicial + Crédito do período (R$ {total_cred:,.2f}) "
        f"- Descontado em períodos anteriores (R$ {total_desc_ant:,.2f}) "
        f"- Descontado no período atual (R$ {total_desc_pa:,.2f}) "
        f"= Saldo final (R$ {total_sld:,.2f})."
    )
    achados.append(_achado(
        "G4", bloco, registro, competencia, "INFORMATIVO",
        descricao,
        total_sld, BASE_LEGAL_CONTROLE_CREDITO,
        "Envie também o(s) arquivo(s) SPED do(s) mês(es) seguinte(s) para que o "
        "sistema verifique a transposição de saldo: o saldo final deste período "
        f"(R$ {total_sld:,.2f}) deve aparecer corretamente no próximo período, "
        "refletido no campo vl_cred_desc_pa_ant do registro 1100/1500.",
        severidade="BAIXO",
    ))
    return achados


def gerar_achados_transposicao(periodos: list[tuple[dict, dict[str, pd.DataFrame]]]) -> list[dict]:
    """Recebe uma lista de (header, dfs) ordenada por competência e gera
    achados de transposição de saldo entre registros 1100 (PIS) e 1500
    (COFINS) de períodos consecutivos.

    Fórmula validada: saldo_final(período A) deve ser igual ao saldo inicial
    implícito de período B, ou seja, vl_cred(B) - vl_cred_desc_pa_ant(B)
    deve refletir o sld_cred_final(A) para a mesma origem."""
    achados: list[dict] = []
    if len(periodos) < 2:
        return achados

    for registro, bloco in (("1100", "1"), ("1500", "1")):
        tributo = "PIS" if registro == "1100" else "COFINS"
        for i in range(len(periodos) - 1):
            header_a, dfs_a = periodos[i]
            header_b, dfs_b = periodos[i + 1]
            comp_a = header_a.get("competencia", "")
            comp_b = header_b.get("competencia", "")

            df_a = dfs_a.get(registro)
            df_b = dfs_b.get(registro)
            if df_a is None or df_a.empty:
                continue

            # Monta mapa de período B: (per_apur_cred, cod_cred) → (vl_cred, sld_cred_final)
            mapa_b: dict[tuple[str, str], dict] = {}
            if df_b is not None and not df_b.empty:
                for _, row in df_b.iterrows():
                    chave = (str(row.get("per_apur_cred", "")), str(row.get("cod_cred", "")))
                    mapa_b[chave] = {
                        "vl_cred": parse_decimal(row.get("vl_cred", "0")),
                        "sld_cred_final": parse_decimal(row.get("sld_cred_final", "0")),
                        "vl_cred_desc_pa_ant": parse_decimal(row.get("vl_cred_desc_pa_ant", "0")),
                    }

            for _, row in df_a.iterrows():
                per_apur = str(row.get("per_apur_cred", ""))
                cod_cred = str(row.get("cod_cred", ""))
                sld_a = parse_decimal(row.get("sld_cred_final", "0"))
                vl_a = parse_decimal(row.get("vl_cred", "0"))
                if vl_a <= 0 and sld_a <= 0:
                    continue
                chave = (per_apur, cod_cred)

                if chave not in mapa_b:
                    if sld_a > TOLERANCIA_VALOR:
                        achados.append(_achado(
                            "G4", bloco, registro, comp_b, "RISCO",
                            f"Controle de crédito {tributo} — origem {per_apur} (código "
                            f"{cod_cred}) tinha saldo de R$ {sld_a:,.2f} em {comp_a} "
                            f"mas não aparece no {registro} de {comp_b}.",
                            sld_a, BASE_LEGAL_CONTROLE_CREDITO,
                            "Verificar se o crédito foi integralmente utilizado ou se foi "
                            "omitido indevidamente. Se ainda há saldo, retificar o SPED de "
                            f"{comp_b} incluindo a linha do período de origem no {registro} "
                            "e apropriar os créditos não aproveitados via M110/M510 "
                            "(crédito extemporâneo, com correção pela SELIC).",
                        ))
                else:
                    info_b = mapa_b[chave]
                    # Saldo inicial em B = vl_cred(B) - vl_cred_desc_pa_ant(B);
                    # deve corresponder ao sld_cred_final(A)
                    saldo_inicial_b = info_b["vl_cred"] - info_b["vl_cred_desc_pa_ant"]
                    diff = saldo_inicial_b - sld_a
                    if abs(diff) > TOLERANCIA_VALOR:
                        achados.append(_achado(
                            "G4", bloco, registro, comp_b, "INCONSISTENCIA",
                            f"Transposição de saldo {tributo} — origem {per_apur} (código "
                            f"{cod_cred}): saldo final em {comp_a} era R$ {sld_a:,.2f}, "
                            f"mas o saldo inicial implícito em {comp_b} é R$ {saldo_inicial_b:,.2f} "
                            f"(diferença de R$ {abs(diff):,.2f}).",
                            abs(diff), BASE_LEGAL_CONTROLE_CREDITO,
                            "Fórmula esperada: saldo_final(período anterior) = "
                            "vl_cred(período atual) - vl_cred_desc_pa_ant(período atual). "
                            "Identificar se houve ajuste não documentado e corrigir "
                            "o campo vl_cred_desc_pa_ant no registro 1100/1500 via retificação.",
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
    participante_tipo_map = build_participante_tipo_map(dfs)

    achados: list[dict] = []
    achados += _g1_creditos_acf(dfs, header, item_map, conta_map, participante_tipo_map)
    achados += _g2_ativo_imobilizado(dfs, header, conta_map)
    achados += _g3_reconciliacao_m(dfs, header)
    achados += _resumo_1100_1500(dfs, "1100", header.get("competencia", ""))
    achados += _resumo_1100_1500(dfs, "1500", header.get("competencia", ""))
    achados += _g5_rateio(dfs, header)
    return achados
