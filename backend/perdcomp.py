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
    s = re.sub(r'[R$\s]', '', s).replace('.', '').replace(',', '.')
    try:
        return float(s)
    except Exception:
        return 0.0

def extrair_texto(file_bytes: bytes) -> str:
    if not PDF_OK:
        return ""
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        return ""

# ─── EXTRAÇÃO DE CAMPOS ───────────────────────────────────────────────────────

def primeiro_match(texto: str, padroes: list) -> str | None:
    for pat in padroes:
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None

def extrair_registro(texto: str, nome: str) -> dict:
    tl = texto.lower()

    # Número
    numero = primeiro_match(texto, [
        r'N[úu]mero do Processo[:\s]+([A-Z0-9\-\/\.]{5,30})',
        r'N[úu]mero PER/DCOMP[:\s]+([A-Z0-9\-\/\.]{5,30})',
        r'Processo n[°º.][:\s]+([A-Z0-9\-\/\.]{5,30})',
        r'N[°º][:\s]+([A-Z0-9\-\/\.]{5,30})',
    ])

    # Tipo
    if 'ressarcimento' in tl:
        tipo = 'Ressarcimento'
    elif 'restituição' in tl or 'restituicao' in tl:
        tipo = 'Restituição'
    elif 'compensação' in tl or 'compensacao' in tl:
        tipo = 'Compensação'
    else:
        tipo = 'PER/DCOMP'

    # Tributo
    tributo = None
    for t in ['PIS', 'COFINS', 'IRPJ', 'CSLL', 'IPI', 'INSS', 'IRRF', 'CSRF']:
        if t in texto.upper():
            tributo = t
            break

    # Competência
    competencia = primeiro_match(texto, [
        r'Per[íi]odo de Apura[çc][aã]o[:\s]+(\d{2}/\d{4})',
        r'Compet[eê]ncia[:\s]+(\d{2}/\d{4})',
        r'M[eê]s/Ano[:\s]+(\d{2}/\d{4})',
        r'(\d{2}/\d{4})',
    ])

    # Valores
    def val(pats):
        v = primeiro_match(texto, pats)
        return parse_valor(v) if v else 0.0

    valor_credito = val([
        r'Valor do Cr[eé]dito[:\s]+([\d\.]+,\d{2})',
        r'Total do Cr[eé]dito[:\s]+([\d\.]+,\d{2})',
        r'Cr[eé]dito Apurado[:\s]+([\d\.]+,\d{2})',
    ])
    valor_compensado = val([
        r'Valor Compensado[:\s]+([\d\.]+,\d{2})',
        r'Total Compensado[:\s]+([\d\.]+,\d{2})',
        r'Valor da Compensa[çc][aã]o[:\s]+([\d\.]+,\d{2})',
    ])
    valor_ressarcido = val([
        r'Valor Ressarcido[:\s]+([\d\.]+,\d{2})',
        r'Valor Restitu[íi]do[:\s]+([\d\.]+,\d{2})',
        r'Total Ressarcido[:\s]+([\d\.]+,\d{2})',
    ])
    saldo = val([
        r'Saldo Remanescente[:\s]+([\d\.]+,\d{2})',
        r'Saldo a Compensar[:\s]+([\d\.]+,\d{2})',
        r'Saldo Dispon[íi]vel[:\s]+([\d\.]+,\d{2})',
    ])
    if saldo == 0 and valor_credito > 0:
        saldo = max(0.0, valor_credito - valor_compensado - valor_ressarcido)

    # Datas
    data_tx = primeiro_match(texto, [
        r'Data de Transmiss[aã]o[:\s]+(\d{2}/\d{2}/\d{4})',
        r'Transmitido em[:\s]+(\d{2}/\d{2}/\d{4})',
        r'Data de Envio[:\s]+(\d{2}/\d{2}/\d{4})',
    ])

    # Situação
    situacao = None
    for s in ['Deferido', 'Indeferido', 'Cancelado', 'Em Análise', 'Ativo', 'Pendente', 'Em processamento']:
        if s.lower() in tl:
            situacao = s
            break

    retificador = 'retificador' in tl or 'retificadora' in tl

    return {
        "arquivo":           nome,
        "numero":            numero,
        "tipo":              tipo,
        "tributo":           tributo,
        "competencia":       competencia,
        "valor_credito":     round(valor_credito, 2),
        "valor_compensado":  round(valor_compensado, 2),
        "valor_ressarcido":  round(valor_ressarcido, 2),
        "saldo_remanescente":round(saldo, 2),
        "data_transmissao":  data_tx,
        "situacao":          situacao,
        "retificador":       retificador,
    }

# ─── MOTOR DE COMPLIANCE ──────────────────────────────────────────────────────

def analisar_compliance(registros: list) -> list:
    alertas = []
    hoje = date.today()

    # R1 — Possível dupla utilização (mesmo número de processo base)
    numeros_vistos: dict[str, dict] = {}
    for r in registros:
        if not r["numero"]:
            continue
        base = r["numero"].split('-')[0]
        if base in numeros_vistos:
            alertas.append({
                "nivel": "alto",
                "tipo": "Possível Dupla Utilização",
                "descricao": f"Número '{r['numero']}' aparece em mais de um documento — risco de reutilização de crédito.",
                "arquivos": [numeros_vistos[base]["arquivo"], r["arquivo"]],
            })
        else:
            numeros_vistos[base] = r

    # R2 — Valor utilizado acima do crédito
    for r in registros:
        utilizado = r["valor_compensado"] + r["valor_ressarcido"]
        if r["valor_credito"] > 0 and utilizado > r["valor_credito"] * 1.005:
            alertas.append({
                "nivel": "alto",
                "tipo": "Crédito Acima do Saldo",
                "descricao": (f"{r['arquivo']}: Valor utilizado R$ {utilizado:,.2f} supera o crédito apurado "
                              f"R$ {r['valor_credito']:,.2f}."),
                "arquivos": [r["arquivo"]],
            })

    # R3 — Saldo negativo
    for r in registros:
        if r["saldo_remanescente"] < -0.01:
            alertas.append({
                "nivel": "alto",
                "tipo": "Saldo Negativo",
                "descricao": f"{r['arquivo']}: Saldo remanescente negativo (R$ {r['saldo_remanescente']:,.2f}) — verificar aritmética.",
                "arquivos": [r["arquivo"]],
            })

    # R4 — Prescrição (> 5 anos) e risco (> 4 anos)
    for r in registros:
        if not r["competencia"]:
            continue
        try:
            partes = r["competencia"].split('/')
            if len(partes) == 2:
                mes, ano = int(partes[0]), int(partes[1])
            else:
                continue
            data_cred = date(ano, mes, 1)
            anos = (hoje - data_cred).days / 365.25
            if anos > 5:
                alertas.append({
                    "nivel": "alto",
                    "tipo": "Risco de Prescrição",
                    "descricao": f"{r['arquivo']}: Crédito de {r['competencia']} tem {anos:.1f} anos — pode estar prescrito (prazo legal: 5 anos).",
                    "arquivos": [r["arquivo"]],
                })
            elif anos > 4:
                alertas.append({
                    "nivel": "medio",
                    "tipo": "Crédito Próximo da Prescrição",
                    "descricao": f"{r['arquivo']}: Crédito de {r['competencia']} tem {anos:.1f} anos — atenção ao prazo de 5 anos.",
                    "arquivos": [r["arquivo"]],
                })
        except Exception:
            pass

    # R5 — Dados incompletos
    for r in registros:
        falta = []
        if not r["tributo"]:      falta.append("tributo")
        if not r["competencia"]:  falta.append("competência")
        if r["valor_credito"] == 0: falta.append("valor do crédito")
        if falta:
            alertas.append({
                "nivel": "medio",
                "tipo": "Informação Incompleta",
                "descricao": f"{r['arquivo']}: Não foi possível extrair {', '.join(falta)}. Revisão manual recomendada.",
                "arquivos": [r["arquivo"]],
            })

    # R6 — Documentos retificadores
    for r in registros:
        if r["retificador"]:
            alertas.append({
                "nivel": "info",
                "tipo": "Documento Retificador",
                "descricao": f"{r['arquivo']}: Documento retificador identificado — confirme se o original correspondente está incluído na análise.",
                "arquivos": [r["arquivo"]],
            })

    # R7 — Pedidos indeferidos ou cancelados
    for r in registros:
        if r["situacao"] in ("Indeferido", "Cancelado"):
            alertas.append({
                "nivel": "medio",
                "tipo": f"Pedido {r['situacao']}",
                "descricao": f"{r['arquivo']}: Status '{r['situacao']}' — verificar se houve recurso ou retificação.",
                "arquivos": [r["arquivo"]],
            })

    return alertas

# ─── ROTAS ────────────────────────────────────────────────────────────────────

@bp.route('/api/perdcomp/health')
def health():
    return jsonify({"ok": True, "pdfplumber": PDF_OK})

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
            if not texto.strip():
                erros.append({"arquivo": f.filename,
                              "erro": "PDF sem texto extraível — pode ser escaneado (OCR não suportado nesta versão)"})
                continue
            registros.append(extrair_registro(texto, f.filename))
        except Exception as e:
            erros.append({"arquivo": f.filename, "erro": str(e)[:200]})

    if not registros and erros:
        return jsonify({"error": "Nenhum arquivo processado", "detalhes": erros}), 400

    alertas = analisar_compliance(registros)

    total_credito    = sum(r["valor_credito"]     for r in registros)
    total_compensado = sum(r["valor_compensado"]  for r in registros)
    total_ressarcido = sum(r["valor_ressarcido"]  for r in registros)
    saldo_total      = sum(r["saldo_remanescente"] for r in registros)

    dist_tributos: dict[str, float] = {}
    dist_tipos:    dict[str, int]   = {}
    for r in registros:
        t  = r["tributo"] or "Não identificado"
        tp = r["tipo"]    or "Não identificado"
        dist_tributos[t]  = dist_tributos.get(t, 0)  + r["valor_credito"]
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
