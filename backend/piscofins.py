"""
Motor de Análise Tributária PIS/COFINS — Regime Não Cumulativo
Leis nº 10.637/2002 e nº 10.833/2003
"""
import io
import uuid
from datetime import datetime

import pandas as pd
from flask import Blueprint, jsonify, request, send_file

bp = Blueprint("piscofins", __name__, url_prefix="/api/piscofins")

_analyses: dict[str, dict] = {}

PIS_RATE = 1.65
COFINS_RATE = 7.6
COMBINED_RATE = PIS_RATE + COFINS_RATE  # 9.25%

# ─── Base de Regras Tributárias ───────────────────────────────────────────────
# Cada regra: keywords (termos a buscar na conta+descrição), categoria, tipo de crédito,
# elegibilidade (SIM / NÃO / POSSÍVEL), risco (BAIXO / MÉDIO / ALTO),
# base legal e comentário técnico.

RULES = [
    # ── INSUMOS DIRETOS ──────────────────────────────────────────────────────
    {
        "keywords": ["matéria-prima", "materia-prima", "materia prima", "matéria prima",
                     "material direto", "material de produção", "material de producao",
                     "insumo", "componente", "embalagem", "embalagens",
                     "material de embalagem", "mp -", "m.p.", "matérias-primas"],
        "category": "Insumos de Produção",
        "credit_type": "Insumo",
        "eligible": "SIM",
        "risk": "BAIXO",
        "legal_basis": "Art. 3º, II, Leis 10.637/2002 e 10.833/2003; STJ REsp 1.221.170/PR (Tema 779)",
        "comment": "Insumos essenciais/relevantes ao processo produtivo geram crédito pleno. Conceito expandido pelo STJ pelo critério da essencialidade e relevância — inclui tudo que impacta direta ou indiretamente a produção.",
        "law_type": "lei + jurisprudência",
    },
    # ── ENERGIA ELÉTRICA ─────────────────────────────────────────────────────
    {
        "keywords": ["energia elétrica", "energia eletrica", "energia e", "eletricidade",
                     "cpfl", "celesc", "cemig", "coelba", "eletropaulo", "enel ", "copel",
                     "light s", "concessionária de energia", "fornecimento de energia",
                     "conta de energia", "gasto com energia"],
        "category": "Energia Elétrica",
        "credit_type": "Energia Elétrica",
        "eligible": "SIM",
        "risk": "BAIXO",
        "legal_basis": "Art. 3º, III, Lei 10.833/2003",
        "comment": "Energia elétrica consumida nos estabelecimentos da pessoa jurídica é expressamente listada como geradora de crédito. Abrange energia consumida na produção e nas instalações administrativas vinculadas à atividade.",
        "law_type": "lei",
    },
    # ── ALUGUÉIS ─────────────────────────────────────────────────────────────
    {
        "keywords": ["aluguel", "locação de imóvel", "locacao de imovel", "aluguer",
                     "arrendamento de imóvel", "arrendamento de imovel",
                     "locação predial", "aluguel de prédio", "aluguel de galpão"],
        "category": "Aluguéis de Imóveis",
        "credit_type": "Aluguel",
        "eligible": "SIM",
        "risk": "BAIXO",
        "legal_basis": "Art. 3º, IV, Lei 10.833/2003",
        "comment": "Aluguéis de prédios pagos a pessoas jurídicas, utilizados nas atividades da empresa, geram crédito. Aluguel pago a pessoa física não gera crédito.",
        "law_type": "lei",
    },
    # ── LEASING / ARRENDAMENTO DE MÁQUINAS ───────────────────────────────────
    {
        "keywords": ["leasing", "arrendamento mercantil", "lease", "arrendamento de máquina",
                     "arrendamento de equipamento", "arrendamento de veículo"],
        "category": "Leasing / Arrendamento Mercantil",
        "credit_type": "Arrendamento Mercantil",
        "eligible": "SIM",
        "risk": "BAIXO",
        "legal_basis": "Art. 3º, V, Lei 10.833/2003",
        "comment": "Arrendamento mercantil de máquinas, equipamentos e outros bens incorporados ao ativo imobilizado, contratado com pessoa jurídica (exceto optante pelo Simples), gera crédito.",
        "law_type": "lei",
    },
    # ── DEPRECIAÇÃO ───────────────────────────────────────────────────────────
    {
        "keywords": ["depreciação", "depreciacao", "amortização", "amortizacao",
                     "depr. acumulada", "depr acumulada", "quota de depreciação"],
        "category": "Depreciação / Amortização",
        "credit_type": "Depreciação",
        "eligible": "SIM",
        "risk": "BAIXO",
        "legal_basis": "Art. 3º, VI e VII, Lei 10.833/2003",
        "comment": "Encargos de depreciação de máquinas, equipamentos, edificações e outros bens incorporados ao ativo imobilizado, adquiridos para uso na produção ou prestação de serviços, geram crédito. Taxa conforme RFB.",
        "law_type": "lei",
    },
    # ── FRETE SOBRE VENDAS ────────────────────────────────────────────────────
    {
        "keywords": ["frete sobre vendas", "frete de vendas", "frete saída", "frete s/ venda",
                     "frete de entrega", "frete outbound", "fretes sobre vendas",
                     "despesa com frete"],
        "category": "Frete sobre Vendas",
        "credit_type": "Frete",
        "eligible": "SIM",
        "risk": "BAIXO",
        "legal_basis": "Art. 3º, IX, Lei 10.833/2003",
        "comment": "Fretes pagos a PJ na venda de produtos destinados ao exterior ou no transporte dentro do território nacional, quando o ônus é suportado pelo vendedor, geram crédito.",
        "law_type": "lei",
    },
    # ── FRETE SOBRE COMPRAS ───────────────────────────────────────────────────
    {
        "keywords": ["frete sobre compras", "frete compras", "frete entrada", "frete inbound",
                     "fretes e carretos", "fretes"],
        "category": "Fretes (Compras)",
        "credit_type": "Frete",
        "eligible": "POSSÍVEL",
        "risk": "MÉDIO",
        "legal_basis": "Art. 3º, II, Lei 10.833/2003; SC COSIT nº 23/2020",
        "comment": "Fretes de compra podem gerar crédito quando vinculados a insumos essenciais ao processo produtivo. A SC COSIT 23/2020 admite o crédito quando o frete integra o custo do insumo. Verificar destinação.",
        "law_type": "lei + SC",
    },
    # ── MANUTENÇÃO E REPAROS ──────────────────────────────────────────────────
    {
        "keywords": ["manutenção", "manutencao", "conservação", "conservacao",
                     "reparo", "revisão de máquinas", "manutenção preventiva",
                     "manutenção corretiva", "peças de reposição", "manutenção industrial"],
        "category": "Manutenção e Reparos",
        "credit_type": "Manutenção (Insumo)",
        "eligible": "POSSÍVEL",
        "risk": "MÉDIO",
        "legal_basis": "Art. 3º, II, Lei 10.833/2003; SC COSIT nº 101/2021",
        "comment": "Serviços de manutenção de máquinas e equipamentos utilizados na produção podem gerar crédito como insumo. A SC COSIT 101/2021 admite crédito para manutenção essencial ao processo. Requer comprovação de essencialidade.",
        "law_type": "lei + SC",
    },
    # ── COMBUSTÍVEIS E LUBRIFICANTES ──────────────────────────────────────────
    {
        "keywords": ["combustível", "combustivel", "diesel", "gasolina", "etanol", "gnv",
                     "óleo diesel", "oleo diesel", "lubrificantes", "combustíveis"],
        "category": "Combustíveis e Lubrificantes",
        "credit_type": "Insumo",
        "eligible": "POSSÍVEL",
        "risk": "MÉDIO",
        "legal_basis": "Art. 3º, II, Lei 10.833/2003; SC COSIT nº 14/2021",
        "comment": "Combustíveis utilizados no processo produtivo ou na prestação de serviços podem gerar crédito. Uso em veículos administrativos não é elegível. Verificar destinação específica.",
        "law_type": "lei + SC",
    },
    # ── SERVIÇOS DE TERCEIROS (PRODUÇÃO) ─────────────────────────────────────
    {
        "keywords": ["terceirização", "terceirizado", "serviços terceirizados",
                     "mão de obra terceirizada", "mao de obra terceirizada",
                     "serviços de terceiros", "prestação de serviços industriais",
                     "serviço industrial", "beneficiamento"],
        "category": "Serviços Terceirizados (Produção)",
        "credit_type": "Insumo / Serviço",
        "eligible": "POSSÍVEL",
        "risk": "MÉDIO",
        "legal_basis": "Art. 3º, II, Lei 10.833/2003; STJ REsp 1.221.170/PR",
        "comment": "Serviços de terceiros aplicados diretamente na produção ou essenciais ao processo podem gerar crédito sob o conceito ampliado de insumo. Exige análise de essencialidade e relevância. Documentação robusta é recomendada.",
        "law_type": "lei + jurisprudência",
    },
    # ── EPI / SEGURANÇA DO TRABALHO ───────────────────────────────────────────
    {
        "keywords": ["epi", "equipamento de proteção individual", "segurança do trabalho",
                     "uniforme", "cipa", "sesmt", "epc", "proteção coletiva",
                     "equipamento de segurança"],
        "category": "EPI / Segurança do Trabalho",
        "credit_type": "Insumo",
        "eligible": "POSSÍVEL",
        "risk": "MÉDIO",
        "legal_basis": "Art. 3º, II, Lei 10.833/2003; SC COSIT nº 34/2014",
        "comment": "EPIs e uniformes obrigatórios utilizados diretamente no processo produtivo podem ser considerados insumos (SC COSIT 34/2014). Aplicável quando há obrigatoriedade legal de uso (NR-6) e relação direta com a produção.",
        "law_type": "lei + SC",
    },
    # ── ÁGUA E SANEAMENTO ────────────────────────────────────────────────────
    {
        "keywords": ["água", "agua", "sabesp", "caesb", "embasa", "sanepar", "esgoto",
                     "saneamento", "concessionária de água", "fornecimento de água",
                     "abastecimento de água"],
        "category": "Água e Saneamento",
        "credit_type": "Utilidades (Insumo)",
        "eligible": "POSSÍVEL",
        "risk": "MÉDIO",
        "legal_basis": "Art. 3º, II, Lei 10.833/2003; SC COSIT nº 218/2019",
        "comment": "Água consumida no processo produtivo pode ser reconhecida como insumo (SC COSIT 218/2019). Uso exclusivamente administrativo não gera crédito. Verificar proporção produtiva/administrativa.",
        "law_type": "lei + SC",
    },
    # ── SEGUROS (ATIVOS PRODUTIVOS) ───────────────────────────────────────────
    {
        "keywords": ["seguro de máquinas", "seguro de equipamentos", "seguro patrimonial",
                     "seguro de instalações", "seguro industrial", "seguro de carga",
                     "prêmio de seguro"],
        "category": "Seguros (Ativos Produtivos)",
        "credit_type": "Insumo",
        "eligible": "POSSÍVEL",
        "risk": "MÉDIO",
        "legal_basis": "Art. 3º, II, Lei 10.833/2003; controvérsia na RFB",
        "comment": "Seguros sobre bens utilizados diretamente na produção podem ser considerados insumos com base no critério da essencialidade. Há controvérsia na RFB — recomenda-se análise caso a caso com suporte de parecer técnico.",
        "law_type": "lei + controvérsia",
    },
    # ── TECNOLOGIA DA INFORMAÇÃO ──────────────────────────────────────────────
    {
        "keywords": ["software", "licença de software", "ti ", "tecnologia da informação",
                     "sistema erp", "cloud", "saas", "infraestrutura de ti",
                     "data center", "licença de uso", "sistemas de gestão"],
        "category": "Tecnologia da Informação",
        "credit_type": "Insumo / Serviço",
        "eligible": "POSSÍVEL",
        "risk": "MÉDIO",
        "legal_basis": "Art. 3º, II, Lei 10.833/2003; jurisprudência em consolidação",
        "comment": "Jurisprudência crescente (CARF e TRFs) reconhece crédito sobre serviços de TI essenciais ao processo produtivo ou prestação de serviços. Análise de essencialidade é obrigatória. ERP operacional tem melhor posição que sistemas administrativos.",
        "law_type": "jurisprudência",
    },
    # ── TELECOMUNICAÇÕES ──────────────────────────────────────────────────────
    {
        "keywords": ["telefone", "telefonia", "telecomunicação", "telecomunicacao",
                     "internet", "banda larga", "celular", "claro", "vivo", "tim ",
                     "oi ", "telefônica", "link dedicado", "fibra óptica"],
        "category": "Telecomunicações",
        "credit_type": "Utilidades",
        "eligible": "POSSÍVEL",
        "risk": "ALTO",
        "legal_basis": "Art. 3º, II, Lei 10.833/2003; controvérsia significativa na RFB",
        "comment": "Telefonia e internet geram crédito apenas quando comprovadamente essenciais ao processo produtivo. Uso comercial/administrativo é questionado. A RFB tem autuado créditos sobre telecomunicações de uso geral — risco elevado sem comprovação de essencialidade direta.",
        "law_type": "lei + controvérsia",
    },
    # ── FOLHA DE PAGAMENTO ────────────────────────────────────────────────────
    {
        "keywords": ["salário", "salario", "salários", "salarios", "folha de pagamento",
                     "remuneração", "remuneracao", "vencimentos", "ordenados",
                     "pro labore", "13°", "13 salário", "férias", "ferias",
                     "rescisão", "rescisao", "aviso prévio", "aviso previo",
                     "benefícios a empregados", "vale refeição", "vale alimentação",
                     "vale transporte", "assistência médica", "plano de saúde",
                     "seguro de vida", "previdência privada", "fgts",
                     "encargos sociais", "inss patronal", "inss "],
        "category": "Folha de Pagamento / RH",
        "credit_type": "N/A",
        "eligible": "NÃO",
        "risk": "BAIXO",
        "legal_basis": "Art. 3º, §2º, I, Leis 10.637/2002 e 10.833/2003",
        "comment": "Mão de obra remunerada por empregados é expressamente excluída do direito a crédito por lei. Inclui salários, encargos, benefícios e quaisquer verbas trabalhistas.",
        "law_type": "lei",
    },
    # ── IRPJ / CSLL ──────────────────────────────────────────────────────────
    {
        "keywords": ["irpj", "csll", "imposto de renda", "irrf sobre", "provisão irpj",
                     "provisão csll", "provisao irpj", "provisao csll",
                     "contribuição social sobre lucro"],
        "category": "Tributos sobre o Lucro",
        "credit_type": "N/A",
        "eligible": "NÃO",
        "risk": "BAIXO",
        "legal_basis": "Art. 3º, §2º, II, Leis 10.637/2002 e 10.833/2003",
        "comment": "IRPJ e CSLL são expressamente vedados como base de crédito de PIS/COFINS pela legislação. Créditos indevidos sobre essas bases geram autuação.",
        "law_type": "lei",
    },
    # ── MULTAS E PENALIDADES ──────────────────────────────────────────────────
    {
        "keywords": ["multa", "penalidade", "auto de infração", "multa fiscal",
                     "multa de trânsito", "multa contratual", "indenização", "sinistro"],
        "category": "Multas e Penalidades",
        "credit_type": "N/A",
        "eligible": "NÃO",
        "risk": "ALTO",
        "legal_basis": "Ausência de previsão legal — Art. 3º, Leis 10.637/2002 e 10.833/2003",
        "comment": "Multas e penalidades não geram crédito de PIS/COFINS. Representam saídas não vinculadas à atividade produtiva. Creditamento indevido expõe a empresa a autuações com multa de 75% a 150%.",
        "law_type": "ausência de previsão",
    },
    # ── PUBLICIDADE E MARKETING ───────────────────────────────────────────────
    {
        "keywords": ["publicidade", "propaganda", "marketing", "mídia", "anúncio",
                     "patrocínio", "patrocinio", "branding", "comunicação e marketing",
                     "agência de publicidade", "material de marketing"],
        "category": "Marketing e Publicidade",
        "credit_type": "N/A",
        "eligible": "NÃO",
        "risk": "MÉDIO",
        "legal_basis": "Ausência de previsão no Art. 3º, Lei 10.833/2003",
        "comment": "Despesas de marketing e publicidade não estão previstas no rol de créditos do Art. 3º. A RFB e o CARF têm negado sistematicamente créditos sobre essas despesas. Creditamento gera risco de autuação.",
        "law_type": "ausência de previsão",
    },
    # ── DESPESAS FINANCEIRAS ──────────────────────────────────────────────────
    {
        "keywords": ["juros sobre", "despesa financeira", "encargos financeiros", "iof",
                     "taxa bancária", "spread bancário", "custo financeiro",
                     "variação cambial", "juros de financiamento", "juros bancários"],
        "category": "Despesas Financeiras",
        "credit_type": "N/A",
        "eligible": "NÃO",
        "risk": "BAIXO",
        "legal_basis": "Ausência de previsão para regime geral; IN RFB nº 1.285/2012 restringe a casos específicos",
        "comment": "Despesas financeiras geralmente não geram crédito no regime não cumulativo padrão. Exceção para instituições financeiras sujeitas ao regime diferenciado (IN 1.285/2012).",
        "law_type": "lei",
    },
    # ── MATERIAL DE ESCRITÓRIO / LIMPEZA ─────────────────────────────────────
    {
        "keywords": ["material de escritório", "material escritório", "papelaria",
                     "expediente", "material de expediente", "material de limpeza",
                     "produtos de limpeza", "material de higiene"],
        "category": "Material de Escritório / Limpeza",
        "credit_type": "N/A",
        "eligible": "NÃO",
        "risk": "MÉDIO",
        "legal_basis": "Ausência de previsão no Art. 3º, Lei 10.833/2003",
        "comment": "Material de escritório e limpeza de uso administrativo não gera crédito. Exceção possível para material de limpeza utilizado diretamente no processo produtivo — exige comprovação.",
        "law_type": "ausência de previsão",
    },
    # ── VIAGENS E REPRESENTAÇÃO ───────────────────────────────────────────────
    {
        "keywords": ["viagem", "passagem aérea", "hospedagem", "hotel", "diária",
                     "representação comercial", "reembolso viagem", "despesas de viagem"],
        "category": "Viagens e Representação",
        "credit_type": "N/A",
        "eligible": "NÃO",
        "risk": "MÉDIO",
        "legal_basis": "Ausência de previsão no Art. 3º, Lei 10.833/2003",
        "comment": "Despesas de viagem e representação comercial não estão previstas como geradores de crédito. A RFB não reconhece esses gastos como insumos para fins de PIS/COFINS.",
        "law_type": "ausência de previsão",
    },
    # ── SERVIÇOS ADMINISTRATIVOS / CONSULTORIA ────────────────────────────────
    {
        "keywords": ["honorários", "honorarios", "consultoria", "assessoria",
                     "serviços administrativos", "serviços jurídicos", "advocacia",
                     "serviços contábeis", "contabilidade", "auditoria"],
        "category": "Serviços Administrativos / Consultoria",
        "credit_type": "N/A",
        "eligible": "NÃO",
        "risk": "MÉDIO",
        "legal_basis": "Ausência de previsão no Art. 3º, Lei 10.833/2003; CARF consolidado",
        "comment": "Serviços administrativos, jurídicos e de consultoria em geral não geram crédito. O CARF tem negado créditos sobre serviços não vinculados diretamente à atividade produtiva.",
        "law_type": "ausência de previsão",
    },
    # ── RECEITAS / FATURAMENTO ────────────────────────────────────────────────
    {
        "keywords": ["receita bruta", "faturamento", "receita de vendas", "vendas brutas",
                     "receita líquida", "receita operacional", "receita"],
        "category": "Receitas",
        "credit_type": "N/A",
        "eligible": "NÃO",
        "risk": "BAIXO",
        "legal_basis": "N/A — Conta de receita, não de despesa/custo",
        "comment": "Contas de receita representam a base de cálculo do PIS/COFINS — não geram crédito. São o ponto de partida do débito, não da apuração de créditos.",
        "law_type": "N/A",
    },
    # ── CUSTOS DE PRODUÇÃO / CPV ──────────────────────────────────────────────
    {
        "keywords": ["cpv", "cogs", "custo de produção", "custo industrial",
                     "custo de fabricação", "custo dos produtos vendidos",
                     "custo de mercadorias", "overhead industrial", "cif", "cip"],
        "category": "Custos de Produção (Genérico)",
        "credit_type": "Insumo",
        "eligible": "POSSÍVEL",
        "risk": "MÉDIO",
        "legal_basis": "Art. 3º, II, Lei 10.833/2003",
        "comment": "Custos de produção agrupados merecem desmembramento e análise individualizada. O rateio de custos indiretos de fabricação pode gerar crédito proporcional. Recomenda-se mapeamento item a item.",
        "law_type": "lei",
    },
    # ── SERVIÇOS GERAIS (CATCHALL) ────────────────────────────────────────────
    {
        "keywords": ["serviços prestados", "serviços gerais", "prestação de serviços",
                     "serviços de"],
        "category": "Serviços Gerais",
        "credit_type": "Insumo / Serviço",
        "eligible": "POSSÍVEL",
        "risk": "MÉDIO",
        "legal_basis": "Art. 3º, II, Lei 10.833/2003",
        "comment": "Serviços em geral podem gerar crédito se comprovada a essencialidade/relevância ao processo produtivo ou prestação de serviços. Requer análise individual de cada contrato.",
        "law_type": "lei + jurisprudência",
    },
]


def _find_rule(conta: str, descricao: str) -> dict | None:
    text = f"{conta} {descricao}".lower()
    for rule in RULES:
        for kw in rule["keywords"]:
            if kw.lower() in text:
                return rule
    return None


def _parse_value(raw) -> float:
    try:
        s = str(raw).strip()
        if not s or s in ("nan", "None", ""):
            return 0.0
        s = s.replace("R$", "").replace(" ", "")
        # Handle Brazilian number format (1.234,56)
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _normalize_cols(df: pd.DataFrame) -> dict:
    """Map semantic column roles to actual DataFrame column names."""
    col_map = {}
    for col in df.columns:
        cl = col.lower().strip()
        if "conta" in cl or "código" in cl or "codigo" in cl or "cód" in cl:
            col_map.setdefault("conta", col)
        elif any(k in cl for k in ["descriç", "descricao", "description", "nome da conta", "nome"]):
            col_map.setdefault("descricao", col)
        elif any(k in cl for k in ["valor", "saldo", "value", "amount", "montante"]):
            col_map.setdefault("valor", col)
        elif any(k in cl for k in ["classif", "nature", "natureza", "grupo"]):
            col_map.setdefault("classificacao", col)

    cols = list(df.columns)
    if "conta" not in col_map and len(cols) > 0:
        col_map["conta"] = cols[0]
    if "descricao" not in col_map and len(cols) > 1:
        col_map["descricao"] = cols[1]
    if "valor" not in col_map and len(cols) > 2:
        col_map["valor"] = cols[2]

    return col_map


def analyze_balancete(df: pd.DataFrame) -> dict:
    col_map = _normalize_cols(df)
    rows_out = []

    for _, row in df.iterrows():
        conta = str(row.get(col_map.get("conta", ""), "")).strip()
        descricao = str(row.get(col_map.get("descricao", ""), "")).strip()
        valor = _parse_value(row.get(col_map.get("valor", ""), 0))

        if not conta and not descricao:
            continue

        rule = _find_rule(conta, descricao)

        if rule is None:
            rule = {
                "category": "Não Classificado",
                "credit_type": "Indeterminado",
                "eligible": "POSSÍVEL",
                "risk": "MÉDIO",
                "legal_basis": "Requer análise manual",
                "comment": "Conta não identificada pelo motor de regras. Recomenda-se revisão manual por especialista tributário com base no conceito de insumo do STJ.",
                "law_type": "manual",
            }

        credit_value = 0.0
        if rule["eligible"] in ("SIM", "POSSÍVEL") and valor > 0:
            credit_value = round(valor * COMBINED_RATE / 100, 2)

        rows_out.append({
            "conta": conta,
            "descricao": descricao,
            "valor": valor,
            "category": rule["category"],
            "credit_type": rule["credit_type"],
            "eligible": rule["eligible"],
            "risk": rule["risk"],
            "legal_basis": rule["legal_basis"],
            "comment": rule["comment"],
            "law_type": rule.get("law_type", ""),
            "credit_value": credit_value,
        })

    # ── Aggregates ──────────────────────────────────────────────────────────
    eligible_sim = [r for r in rows_out if r["eligible"] == "SIM" and r["valor"] > 0]
    eligible_pos = [r for r in rows_out if r["eligible"] == "POSSÍVEL" and r["valor"] > 0]

    total_analisado = sum(r["valor"] for r in rows_out if r["valor"] > 0)
    total_sim = sum(r["valor"] for r in eligible_sim)
    total_pos = sum(r["valor"] for r in eligible_pos)
    credito_certo = sum(r["credit_value"] for r in eligible_sim)
    credito_possivel = sum(r["credit_value"] for r in eligible_pos)

    pct = round(total_sim / total_analisado * 100, 1) if total_analisado else 0

    # By category
    by_cat: dict[str, dict] = {}
    for r in rows_out:
        cat = r["category"]
        if cat not in by_cat:
            by_cat[cat] = {"valor": 0.0, "credito": 0.0,
                           "eligible": r["eligible"], "risk": r["risk"]}
        if r["valor"] > 0:
            by_cat[cat]["valor"] += r["valor"]
        by_cat[cat]["credito"] += r["credit_value"]

    # By credit type (only eligible)
    by_type: dict[str, dict] = {}
    for r in [r for r in rows_out if r["eligible"] in ("SIM", "POSSÍVEL") and r["valor"] > 0]:
        ct = r["credit_type"]
        if ct not in by_type:
            by_type[ct] = {"valor": 0.0, "credito": 0.0}
        by_type[ct]["valor"] += r["valor"]
        by_type[ct]["credito"] += r["credit_value"]

    # Risk distribution (by value)
    risk_dist = {"ALTO": 0.0, "MÉDIO": 0.0, "BAIXO": 0.0}
    for r in rows_out:
        lvl = r["risk"]
        risk_dist[lvl] = risk_dist.get(lvl, 0.0) + abs(r["valor"])

    top_oportunidades = sorted(
        [r for r in rows_out if r["eligible"] in ("SIM", "POSSÍVEL") and r["credit_value"] > 0],
        key=lambda x: x["credit_value"],
        reverse=True,
    )[:10]

    top_riscos = sorted(
        [r for r in rows_out if r["risk"] == "ALTO" and abs(r["valor"]) > 0],
        key=lambda x: abs(x["valor"]),
        reverse=True,
    )[:10]

    return {
        "rows": rows_out,
        "summary": {
            "total_analisado": round(total_analisado, 2),
            "total_elegivel": round(total_sim, 2),
            "total_possivel": round(total_pos, 2),
            "credito_certo": round(credito_certo, 2),
            "credito_possivel": round(credito_possivel, 2),
            "credito_total_potencial": round(credito_certo + credito_possivel, 2),
            "pct_aproveitamento": pct,
            "total_linhas": len(rows_out),
            "pis_rate": PIS_RATE,
            "cofins_rate": COFINS_RATE,
        },
        "by_category": by_cat,
        "by_credit_type": by_type,
        "risk_distribution": {k: round(v, 2) for k, v in risk_dist.items()},
        "top_oportunidades": top_oportunidades,
        "top_riscos": top_riscos,
    }


# ─── Rotas Flask ──────────────────────────────────────────────────────────────

@bp.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400

    f = request.files["file"]
    if not f.filename.lower().endswith(".xlsx"):
        return jsonify({"error": "Apenas arquivos .xlsx são aceitos"}), 400

    try:
        df = pd.read_excel(f, dtype=str)
    except Exception as e:
        return jsonify({"error": f"Erro ao ler planilha: {e}"}), 400

    if df.empty:
        return jsonify({"error": "Planilha vazia"}), 400

    try:
        result = analyze_balancete(df)
    except Exception as e:
        return jsonify({"error": f"Erro na análise: {e}"}), 500

    analysis_id = str(uuid.uuid4())
    result["analysis_id"] = analysis_id
    result["filename"] = f.filename
    result["created_at"] = datetime.now().isoformat()
    _analyses[analysis_id] = result

    return jsonify({
        "analysis_id": analysis_id,
        "filename": f.filename,
        "summary": result["summary"],
        "by_category": result["by_category"],
        "by_credit_type": result["by_credit_type"],
        "risk_distribution": result["risk_distribution"],
        "top_oportunidades": result["top_oportunidades"],
        "top_riscos": result["top_riscos"],
        "rows": result["rows"],
    })


@bp.route("/download/excel/<analysis_id>")
def download_excel(analysis_id):
    if analysis_id not in _analyses:
        return jsonify({"error": "Análise não encontrada. Refaça o upload."}), 404

    result = _analyses[analysis_id]
    rows = result["rows"]
    summary = result["summary"]
    filename = result.get("filename", "balancete")

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        # ── Aba 1: Análise Detalhada ─────────────────────────────────────────
        df_detail = pd.DataFrame([{
            "Conta Contábil": r["conta"],
            "Descrição": r["descricao"],
            "Valor (R$)": r["valor"],
            "Categoria": r["category"],
            "Elegibilidade": r["eligible"],
            "Tipo de Crédito": r["credit_type"],
            "Crédito PIS/COFINS Potencial (R$)": r["credit_value"],
            "Nível de Risco": r["risk"],
            "Base Legal": r["legal_basis"],
            "Comentário Técnico": r["comment"],
        } for r in rows])
        df_detail.to_excel(writer, sheet_name="Análise Detalhada", index=False)

        # ── Aba 2: Resumo Executivo ──────────────────────────────────────────
        ws_sum = writer.book.create_sheet("Resumo Executivo")
        pct_pis = round(summary["credito_certo"] * PIS_RATE / COMBINED_RATE, 2)
        pct_cof = round(summary["credito_certo"] * COFINS_RATE / COMBINED_RATE, 2)
        summary_rows = [
            ["ANÁLISE TRIBUTÁRIA PIS/COFINS — REGIME NÃO CUMULATIVO"],
            ["Gerado em", datetime.now().strftime("%d/%m/%Y %H:%M")],
            ["Arquivo analisado", filename],
            [""],
            ["VISÃO GERAL"],
            ["Total Analisado (R$)", summary["total_analisado"]],
            ["Base Confirmada para Crédito (R$)", summary["total_elegivel"]],
            ["Base com Potencial de Crédito (R$)", summary["total_possivel"]],
            ["% Aproveitamento Confirmado", f"{summary['pct_aproveitamento']}%"],
            [""],
            ["CRÉDITOS POTENCIAIS"],
            ["Crédito PIS (1,65%) — Confirmado (R$)", pct_pis],
            ["Crédito COFINS (7,6%) — Confirmado (R$)", pct_cof],
            ["Crédito Total Confirmado (R$)", summary["credito_certo"]],
            ["Crédito Total Potencial (R$)", summary["credito_total_potencial"]],
            [""],
            ["IMPORTANTE"],
            ["Esta análise é indicativa. Recomenda-se validação por especialista tributário", ""],
            ["antes de qualquer aproveitamento de crédito junto à Receita Federal.", ""],
        ]
        for rd in summary_rows:
            ws_sum.append(rd)

        # ── Formatação ──────────────────────────────────────────────────────
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter

        ws_d = writer.sheets["Análise Detalhada"]
        hfill = PatternFill("solid", start_color="1B3A5C")
        hfont = Font(bold=True, color="FFFFFF", size=11)
        for c in range(1, len(df_detail.columns) + 1):
            cell = ws_d.cell(row=1, column=c)
            cell.fill = hfill
            cell.font = hfont
            cell.alignment = Alignment(horizontal="center", wrap_text=True)

        color_map = {
            "SIM": PatternFill("solid", start_color="C6EFCE"),
            "NÃO": PatternFill("solid", start_color="FFC7CE"),
            "POSSÍVEL": PatternFill("solid", start_color="FFEB9C"),
        }
        for ri, row in enumerate(rows, start=2):
            fill = color_map.get(row["eligible"], PatternFill("solid", start_color="D9D9D9"))
            for c in range(1, len(df_detail.columns) + 1):
                ws_d.cell(row=ri, column=c).fill = fill

        widths = [20, 45, 16, 28, 14, 26, 28, 14, 65, 90]
        for i, w in enumerate(widths, 1):
            ws_d.column_dimensions[get_column_letter(i)].width = w

        ws_sum.column_dimensions["A"].width = 52
        ws_sum.column_dimensions["B"].width = 24

    output.seek(0)
    dl_name = f"analise_piscofins_{analysis_id[:8]}_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=dl_name,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.route("/health")
def health():
    return jsonify({"status": "ok", "analyses_cached": len(_analyses)})
