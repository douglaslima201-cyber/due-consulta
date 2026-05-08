"""
PER/DCOMP Analyzer — Backend Blueprint
Extração de PDFs + Motor de Compliance
"""
import io
import re
from datetime import date
from flask import Blueprint, request, jsonify

try:
    import pdfplumber
    PDF_OK = True
except ImportError:
    PDF_OK = False

bp = Blueprint('perdcomp', __name__)

# ─── UTILITÁRIOS ──────────────────────────────────────────────────────────────

def parse_valor(s: str) -> float:
    if not s:
        return 0.0
    s = re.sub(r'[R$\s\t]', '', str(s)).replace('.', '').replace(',', '.')
    try:
        return float(s)
    except Exception:
        return 0.0

def extrair_texto(file_bytes: bytes) -> str:
    if not PDF_OK:
        return ""
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            paginas = []
            for p in pdf.pages:
                t = p.extract_text(x_tolerance=3, y_tolerance=3)
                if t:
                    paginas.append(t)
            return "\n".join(paginas)
    except Exception as e:
        return f"ERRO_EXTRACAO: {e}"

def primeiro_match(texto: str, padroes: list, flags=re.IGNORECASE) -> str | None:
    for pat in padroes:
        m = re.search(pat, texto, flags)
        if m:
            return m.group(1).strip()
    return None

def todos_matches(texto: str, padrao: str, flags=re.IGNORECASE) -> list:
    return re.findall(padrao, texto, flags)

# ─── DETECÇÃO DE TIPO ─────────────────────────────────────────────────────────

def detectar_tipo(texto: str, tl: str) -> str:
    # Título e cabeçalho têm prioridade
    primeiras = texto[:600].lower()
    if any(x in primeiras for x in ['declaração de compensação', 'dcomp', 'declaracao de compensacao']):
        return 'Compensação'
    if any(x in primeiras for x in ['pedido de ressarcimento', 'per - ressarcimento', 'ressarcimento']):
        return 'Ressarcimento'
    if any(x in primeiras for x in ['pedido de restituição', 'pedido de restituicao', 'restituição', 'restituicao']):
        return 'Restituição'
    # Fallback no texto completo
    if 'compensação' in tl or 'compensacao' in tl:
        return 'Compensação'
    if 'ressarcimento' in tl:
        return 'Ressarcimento'
    if 'restituição' in tl or 'restituicao' in tl:
        return 'Restituição'
    return 'PER/DCOMP'

def detectar_tributo(texto: str) -> str | None:
    """Detecta tributo considerando variações de nomenclatura da Receita Federal."""
    padroes = [
        (r'PIS[/\s]Pasep', 'PIS'),
        (r'\bPIS\b', 'PIS'),
        (r'COFINS\s+N[ãa]o[- ]Cumulativ', 'COFINS'),
        (r'\bCOFINS\b', 'COFINS'),
        (r'\bCSLL\b', 'CSLL'),
        (r'\bIRPJ\b', 'IRPJ'),
        (r'\bIRRF\b', 'IRRF'),
        (r'\bCSRF\b', 'CSRF'),
        (r'\bIPI\b', 'IPI'),
        (r'\bINSS\b', 'INSS'),
    ]
    for pat, nome in padroes:
        if re.search(pat, texto, re.IGNORECASE):
            return nome
    return None

# ─── EXTRAÇÃO POR TIPO ────────────────────────────────────────────────────────

# Padrões de valor monetário brasileiro
_VAL = r'([\d]{1,3}(?:\.[\d]{3})*,\d{2})'

def _val(texto: str, labels: list) -> float:
    """Busca valor após um label, flexível quanto a espaços e tabs."""
    for label in labels:
        pat = label + r'[\s\t:]*' + _VAL
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            return parse_valor(m.group(1))
    return 0.0

def extrair_dcomp(texto: str, tl: str, nome: str) -> dict:
    """
    DCOMP tem duas seções principais:
    - Crédito objeto da compensação (origem do crédito)
    - Débito objeto da compensação (o que está sendo quitado)
    """
    # Valor do crédito disponível/original
    valor_credito = _val(texto, [
        r'Valor do Cr[eé]dito Dispon[íi]vel',
        r'Valor do Cr[eé]dito Original',
        r'Valor Total do Cr[eé]dito',
        r'Valor do Cr[eé]dito',
        r'Cr[eé]dito Dispon[íi]vel',
    ])

    # Valor efetivamente compensado neste DCOMP
    valor_compensado = _val(texto, [
        r'Valor Compensado',
        r'Valor da Compensa[çc][aã]o',
        r'Valor do D[eé]bito Compensado',
        r'Valor Objeto da Compensa[çc][aã]o',
        r'Valor do D[eé]bito',
    ])

    # Saldo após compensação
    saldo = _val(texto, [
        r'Saldo do Cr[eé]dito ap[oó]s',
        r'Saldo Remanescente',
        r'Saldo a Compensar',
        r'Saldo Dispon[íi]vel',
    ])

    # Se compensado não encontrado mas temos crédito e saldo, inferir
    if valor_compensado == 0 and valor_credito > 0 and saldo >= 0:
        valor_compensado = max(0.0, valor_credito - saldo)

    # Se saldo não encontrado, calcular
    if saldo == 0 and valor_credito > 0:
        saldo = max(0.0, valor_credito - valor_compensado)

    return {
        "valor_credito":      round(valor_credito, 2),
        "valor_compensado":   round(valor_compensado, 2),
        "valor_ressarcido":   0.0,
        "saldo_remanescente": round(saldo, 2),
    }

def extrair_per_ressarcimento(texto: str, tl: str) -> dict:
    valor_credito = _val(texto, [
        r'Valor do Ressarcimento',
        r'Valor Solicitado',
        r'Valor do Cr[eé]dito a Ressarcir',
        r'Valor do Cr[eé]dito',
        r'Total do Cr[eé]dito',
    ])

    valor_ressarcido = _val(texto, [
        r'Valor Ressarcido',
        r'Valor Pago',
        r'Valor Deferido',
    ])

    saldo = _val(texto, [
        r'Saldo Remanescente',
        r'Saldo a Ressarcir',
        r'Saldo Dispon[íi]vel',
    ])
    if saldo == 0 and valor_credito > 0:
        saldo = max(0.0, valor_credito - valor_ressarcido)

    return {
        "valor_credito":      round(valor_credito, 2),
        "valor_compensado":   0.0,
        "valor_ressarcido":   round(valor_ressarcido, 2),
        "saldo_remanescente": round(saldo, 2),
    }

def extrair_per_restituicao(texto: str, tl: str) -> dict:
    valor_credito = _val(texto, [
        r'Valor da Restituição',
        r'Valor da Restituicao',
        r'Valor Solicitado',
        r'Valor do Cr[eé]dito',
        r'Valor a Restituir',
    ])

    valor_ressarcido = _val(texto, [
        r'Valor Restitu[íi]do',
        r'Valor Pago',
        r'Valor Deferido',
    ])

    saldo = _val(texto, [
        r'Saldo Remanescente',
        r'Saldo a Restituir',
    ])
    if saldo == 0 and valor_credito > 0:
        saldo = max(0.0, valor_credito - valor_ressarcido)

    return {
        "valor_credito":      round(valor_credito, 2),
        "valor_compensado":   0.0,
        "valor_ressarcido":   round(valor_ressarcido, 2),
        "saldo_remanescente": round(saldo, 2),
    }

# ─── CAMPOS COMUNS ────────────────────────────────────────────────────────────

def extrair_numero(texto: str) -> str | None:
    return primeiro_match(texto, [
        # Formato longo Receita Federal: 00000.000000/0000-00
        r'N[úu]mero do Processo[:\s]+(\d{5}\.\d{6}/\d{4}-\d{2})',
        r'Processo[:\s]+(\d{5}\.\d{6}/\d{4}-\d{2})',
        # Formato PER/DCOMP antigo
        r'N[úu]mero do PER/DCOMP[:\s]+([A-Z0-9\-\/\.]{5,40})',
        r'N[úu]mero[:\s]+([A-Z0-9\-\/\.]{5,40})',
        # Genérico: sequência alfanumérica longa após keyword
        r'(?:Processo|N[úu]mero)[:\s]+([A-Z0-9]{8,})',
    ])

def extrair_competencia(texto: str) -> str | None:
    return primeiro_match(texto, [
        r'Per[íi]odo de Apura[çc][aã]o[:\s]+(\d{2}/\d{4})',
        r'Per[íi]odo[:\s]+(\d{2}/\d{4})',
        r'Compet[eê]ncia[:\s]+(\d{2}/\d{4})',
        r'M[eê]s/Ano[:\s]+(\d{2}/\d{4})',
        r'Data de Apura[çc][aã]o[:\s]+\d{2}/(\d{2}/\d{4})',
    ])

def extrair_data_tx(texto: str) -> str | None:
    return primeiro_match(texto, [
        r'Data de Transmiss[aã]o[:\s]+(\d{2}/\d{2}/\d{4})',
        r'Transmitido em[:\s]+(\d{2}/\d{2}/\d{4})',
        r'Data de Envio[:\s]+(\d{2}/\d{2}/\d{4})',
        r'Data/Hora[:\s]+(\d{2}/\d{2}/\d{4})',
    ])

def extrair_situacao(texto: str, tl: str) -> str | None:
    for s in ['Deferido', 'Indeferido', 'Cancelado', 'Em Análise',
              'Em análise', 'Ativo', 'Pendente', 'Em processamento',
              'Homologado', 'Não Homologado']:
        if s.lower() in tl:
            return s
    return None

# ─── MONTAGEM DO REGISTRO ─────────────────────────────────────────────────────

def extrair_registro(texto: str, nome: str) -> dict:
    tl = texto.lower()
    tipo = detectar_tipo(texto, tl)

    if tipo == 'Compensação':
        vals = extrair_dcomp(texto, tl, nome)
    elif tipo == 'Ressarcimento':
        vals = extrair_per_ressarcimento(texto, tl)
    elif tipo == 'Restituição':
        vals = extrair_per_restituicao(texto, tl)
    else:
        vals = extrair_dcomp(texto, tl, nome)  # fallback

    return {
        "arquivo":            nome,
        "numero":             extrair_numero(texto),
        "tipo":               tipo,
        "tributo":            detectar_tributo(texto),
        "competencia":        extrair_competencia(texto),
        "data_transmissao":   extrair_data_tx(texto),
        "situacao":           extrair_situacao(texto, tl),
        "retificador":        bool(re.search(r'retificador|retificadora', tl)),
        **vals,
        "_debug_preview":     texto[:1500],   # removido na resposta final
    }

# ─── MOTOR DE COMPLIANCE ──────────────────────────────────────────────────────

def analisar_compliance(registros: list) -> list:
    alertas = []
    hoje = date.today()

    # R1 — Possível dupla utilização (mesmo número base)
    numeros_vistos: dict[str, dict] = {}
    for r in registros:
        if not r.get("numero"):
            continue
        base = re.sub(r'[-/\s]', '', r["numero"])
        if base in numeros_vistos:
            alertas.append({
                "nivel": "alto",
                "tipo": "Possível Dupla Utilização",
                "descricao": f"Número '{r['numero']}' aparece em mais de um documento — risco de reutilização de crédito.",
                "arquivos": [numeros_vistos[base]["arquivo"], r["arquivo"]],
            })
        else:
            numeros_vistos[base] = r

    # R2 — Crédito utilizado acima do disponível
    for r in registros:
        utilizado = r["valor_compensado"] + r["valor_ressarcido"]
        if r["valor_credito"] > 0 and utilizado > r["valor_credito"] * 1.005:
            alertas.append({
                "nivel": "alto",
                "tipo": "Crédito Acima do Saldo",
                "descricao": (f"{r['arquivo']}: Valor utilizado R$ {utilizado:,.2f} supera "
                              f"o crédito apurado R$ {r['valor_credito']:,.2f}."),
                "arquivos": [r["arquivo"]],
            })

    # R3 — Saldo negativo
    for r in registros:
        if r["saldo_remanescente"] < -0.01:
            alertas.append({
                "nivel": "alto",
                "tipo": "Saldo Negativo",
                "descricao": f"{r['arquivo']}: Saldo remanescente negativo (R$ {r['saldo_remanescente']:,.2f}).",
                "arquivos": [r["arquivo"]],
            })

    # R4 — Prescrição e risco de prescrição
    for r in registros:
        comp = r.get("competencia")
        if not comp:
            continue
        try:
            partes = comp.split('/')
            if len(partes) != 2:
                continue
            mes, ano = int(partes[0]), int(partes[1])
            data_cred = date(ano, mes, 1)
            anos = (hoje - data_cred).days / 365.25
            if anos > 5:
                alertas.append({
                    "nivel": "alto",
                    "tipo": "Risco de Prescrição",
                    "descricao": f"{r['arquivo']}: Crédito de {comp} tem {anos:.1f} anos — provável prescrição (prazo: 5 anos).",
                    "arquivos": [r["arquivo"]],
                })
            elif anos > 4:
                alertas.append({
                    "nivel": "medio",
                    "tipo": "Crédito Próximo da Prescrição",
                    "descricao": f"{r['arquivo']}: Crédito de {comp} tem {anos:.1f} anos — monitorar prazo de 5 anos.",
                    "arquivos": [r["arquivo"]],
                })
        except Exception:
            pass

    # R5 — Dados não extraídos (revisão manual)
    for r in registros:
        falta = []
        if not r.get("tributo"):        falta.append("tributo")
        if not r.get("competencia"):    falta.append("competência")
        if r["valor_credito"] == 0 and r["valor_compensado"] == 0:
            falta.append("valores")
        if falta:
            alertas.append({
                "nivel": "medio",
                "tipo": "Dados Não Extraídos",
                "descricao": f"{r['arquivo']}: Não foi possível identificar {', '.join(falta)} — revisão manual necessária.",
                "arquivos": [r["arquivo"]],
            })

    # R6 — Documentos retificadores
    for r in registros:
        if r.get("retificador"):
            alertas.append({
                "nivel": "info",
                "tipo": "Documento Retificador",
                "descricao": f"{r['arquivo']}: Retificador identificado — verificar se o original está incluído na análise.",
                "arquivos": [r["arquivo"]],
            })

    # R7 — Pedidos indeferidos ou cancelados
    for r in registros:
        if r.get("situacao") in ("Indeferido", "Cancelado"):
            alertas.append({
                "nivel": "medio",
                "tipo": f"Pedido {r['situacao']}",
                "descricao": f"{r['arquivo']}: Status '{r['situacao']}' — verificar se houve recurso ou retificação.",
                "arquivos": [r["arquivo"]],
            })

    # R8 — Compensação sem crédito identificado
    for r in registros:
        if r["tipo"] == "Compensação" and r["valor_compensado"] > 0 and r["valor_credito"] == 0:
            alertas.append({
                "nivel": "medio",
                "tipo": "Crédito Origem Não Identificado",
                "descricao": f"{r['arquivo']}: Compensação de R$ {r['valor_compensado']:,.2f} sem crédito origem identificado no PDF.",
                "arquivos": [r["arquivo"]],
            })

    return alertas

# ─── ROTAS ────────────────────────────────────────────────────────────────────

@bp.route('/api/perdcomp/health')
def health():
    return jsonify({"ok": True, "pdfplumber": PDF_OK})

@bp.route('/api/perdcomp/debug', methods=['POST'])
def debug():
    """Retorna o texto bruto extraído dos PDFs para diagnóstico."""
    if not PDF_OK:
        return jsonify({"error": "pdfplumber não instalado"}), 500
    files = request.files.getlist('files')
    resultado = []
    for f in files:
        if not f.filename:
            continue
        texto = extrair_texto(f.read())
        resultado.append({
            "arquivo": f.filename,
            "chars": len(texto),
            "texto": texto[:3000],
            "tipo_detectado": detectar_tipo(texto, texto.lower()),
            "tributo_detectado": detectar_tributo(texto),
            "numero_detectado": extrair_numero(texto),
            "competencia_detectada": extrair_competencia(texto),
        })
    return jsonify(resultado)

@bp.route('/api/perdcomp/upload', methods=['POST'])
def upload():
    if not PDF_OK:
        return jsonify({"error": "pdfplumber não instalado. Execute: pip install pdfplumber"}), 500

    files = request.files.getlist('files')
    if not files or all(not f.filename for f in files):
        return jsonify({"error": "Nenhum arquivo enviado"}), 400

    registros, erros = [], []

    for f in files:
        if not f.filename:
            continue
        try:
            texto = extrair_texto(f.read())
            if not texto.strip() or texto.startswith("ERRO_EXTRACAO"):
                erros.append({"arquivo": f.filename,
                              "erro": texto if texto.startswith("ERRO") else
                              "PDF sem texto extraível — pode ser escaneado (OCR não suportado nesta versão)"})
                continue
            reg = extrair_registro(texto, f.filename)
            reg.pop("_debug_preview", None)
            registros.append(reg)
        except Exception as e:
            erros.append({"arquivo": f.filename, "erro": str(e)[:300]})

    if not registros and erros:
        return jsonify({"error": "Nenhum arquivo processado", "detalhes": erros}), 400

    alertas = analisar_compliance(registros)

    total_credito    = sum(r["valor_credito"]      for r in registros)
    total_compensado = sum(r["valor_compensado"]   for r in registros)
    total_ressarcido = sum(r["valor_ressarcido"]   for r in registros)
    saldo_total      = sum(r["saldo_remanescente"] for r in registros)

    dist_tributos: dict[str, float] = {}
    dist_tipos:    dict[str, int]   = {}
    for r in registros:
        t  = r["tributo"] or "Não identificado"
        tp = r["tipo"]    or "Não identificado"
        # Para distribuição de valores: usa compensado se DCOMP, crédito se PER
        val = r["valor_compensado"] if r["tipo"] == "Compensação" else r["valor_credito"]
        dist_tributos[t]  = dist_tributos.get(t, 0)  + val
        dist_tipos[tp]    = dist_tipos.get(tp, 0)    + 1

    return jsonify({
        "registros": registros,
        "alertas":   alertas,
        "erros":     erros,
        "sumario": {
            "total_arquivos":    len(registros),
            "total_credito":     round(total_credito, 2),
            "total_compensado":  round(total_compensado, 2),
            "total_ressarcido":  round(total_ressarcido, 2),
            "saldo_disponivel":  round(saldo_total, 2),
            "alertas_alto":      sum(1 for a in alertas if a["nivel"] == "alto"),
            "alertas_medio":     sum(1 for a in alertas if a["nivel"] == "medio"),
            "alertas_info":      sum(1 for a in alertas if a["nivel"] == "info"),
            "dist_tributos":     dist_tributos,
            "dist_tipos":        dist_tipos,
        }
    })
