"""
PER/DCOMP Analyzer — Backend Blueprint
Extração de PDFs + Motor de Compliance + Vinculação PER↔DCOMP + Download eCAC
"""
import asyncio
import io
import os
import re
import threading
import uuid
from datetime import date
from pathlib import Path
from flask import Blueprint, request, jsonify, send_from_directory

try:
    import pdfplumber
    PDF_OK = True
except ImportError:
    PDF_OK = False

bp = Blueprint('perdcomp', __name__)

# ─── ECAC DOWNLOAD ────────────────────────────────────────────────────────────
_ECAC_JOBS: dict[str, dict] = {}
_ECAC_DIR  = Path(__file__).parent / "ecac_downloads"
_ECAC_DIR.mkdir(exist_ok=True)
_ECAC_URL  = "https://cav.receita.fazenda.gov.br/eCAC/"
_CHROME_PROFILE = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")

def _elog(job_id: str, msg: str):
    _ECAC_JOBS[job_id]["log"].append(msg)
    print(f"[ECAC][{job_id[:8]}] {msg}")

def _chrome_bloqueado() -> bool:
    lock = Path(_CHROME_PROFILE) / "lockfile"
    return lock.exists()

async def _ecac_download_async(job_id: str, periodo_ini: str, periodo_fim: str):
    from playwright.async_api import async_playwright, TimeoutError as PwTimeout

    job = _ECAC_JOBS[job_id]
    dest = _ECAC_DIR / job_id
    dest.mkdir(exist_ok=True)

    async with async_playwright() as p:
        # ── Abrir nova janela do Chrome (sem fechar o Chrome existente) ────
        try:
            browser = await p.chromium.launch(
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled",
                      "--start-maximized"],
            )
            ctx = await browser.new_context(
                accept_downloads=True,
                downloads_path=str(dest),
                viewport={"width": 1280, "height": 900},
            )
            _elog(job_id, "Nova janela do Chrome aberta para o eCAC.")
        except Exception as exc:
            _elog(job_id, f"Erro ao abrir Chrome: {exc}")
            job["status"] = "erro"; return

        page = await ctx.new_page()
        job["status"] = "aguardando_login"

        # ── Ir para eCAC ───────────────────────────────────────────────────
        try:
            await page.goto(_ECAC_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        _elog(job_id, "Aguardando autenticação com certificado digital (até 5 min)...")

        # Detecta login bem-sucedido: URL muda para área autenticada
        try:
            await page.wait_for_function(
                """() => {
                    const u = location.href.toLowerCase();
                    return (u.includes('ecac') || u.includes('eservicos')) &&
                           !u.includes('login') && !u.includes('acesso.gov');
                }""",
                timeout=300_000,
            )
            _elog(job_id, "Login detectado. Navegando para PER/DCOMP...")
        except PwTimeout:
            _elog(job_id, "Timeout: login não completado em 5 minutos.")
            job["status"] = "erro"; await ctx.close(); return

        job["status"] = "navegando"

        # ── Tentar navegar automaticamente para PER/DCOMP ─────────────────
        nav_ok = False
        for sel in [
            'a[href*="perdcomp" i]', 'a:text-matches("PER/DCOMP", "i")',
            'a:text-matches("Compensação", "i")', 'a:text-matches("Restituição", "i")',
        ]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=3000):
                    await el.click()
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    nav_ok = True
                    _elog(job_id, f"Navegação automática OK ({sel}).")
                    break
            except Exception:
                continue

        if not nav_ok:
            job["status"] = "aguardando_navegacao"
            _elog(job_id, "Navegação automática falhou. Navegue manualmente até a lista de PER/DCOMPs e clique em 'Já estou na lista' no painel.")
            # Aguardar confirmação do usuário via flag
            for _ in range(300):   # 5 min
                await asyncio.sleep(1)
                if job.get("usuario_confirmou"):
                    break
            else:
                _elog(job_id, "Timeout aguardando navegação manual.")
                job["status"] = "erro"; await ctx.close(); return
            job["status"] = "navegando"

        # ── Aplicar filtro de período ──────────────────────────────────────
        _elog(job_id, f"Aplicando filtro: {periodo_ini} a {periodo_fim}...")
        await asyncio.sleep(2)

        async def preencher(sels: list[str], valor: str) -> bool:
            for sel in sels:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2000):
                        await el.fill("")
                        await el.type(valor, delay=40)
                        return True
                except Exception:
                    continue
            return False

        await preencher([
            'input[name*="competenciaInicial" i]', 'input[placeholder*="início" i]',
            'input[id*="inicio" i]', 'input[id*="periodoInicial" i]',
        ], periodo_ini)

        await preencher([
            'input[name*="competenciaFinal" i]', 'input[placeholder*="final" i]',
            'input[id*="fim" i]', 'input[id*="periodoFinal" i]',
        ], periodo_fim)

        # Pesquisar
        for sel in ['button:text-matches("Pesquisar|Consultar|Buscar", "i")',
                    'input[type="submit"]', 'button[type="submit"]']:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await page.wait_for_load_state("networkidle", timeout=20000)
                    _elog(job_id, "Pesquisa executada.")
                    break
            except Exception:
                continue

        await asyncio.sleep(2)

        # ── Coletar e baixar PDFs ──────────────────────────────────────────
        job["status"] = "baixando"
        arquivos_baixados: list[str] = []

        # Selecionar todos os itens (checkbox "Todos", se existir)
        for sel in ['input[id*="todos" i][type="checkbox"]',
                    'input[id*="all" i][type="checkbox"]']:
            try:
                cb = page.locator(sel).first
                if await cb.is_visible(timeout=1500):
                    await cb.check()
                    break
            except Exception:
                pass

        # Links de download individuais
        links = await page.locator(
            'a[href*=".pdf" i], a[href*="download" i], a[href*="imprimir" i], '
            'a:text-matches("PDF|Visualizar|Imprimir|Baixar", "i")'
        ).all()

        if not links:
            _elog(job_id, "Nenhum link de download encontrado. Tente usar o botão 'Exportar Selecionados' manualmente.")
            job["status"] = "aguardando_download_manual"
            # Aguardar up to 10 min que o usuário baixe manualmente para a pasta
            for _ in range(600):
                await asyncio.sleep(1)
                novos = list(dest.glob("*.pdf"))
                if novos and len(novos) > len(arquivos_baixados):
                    _elog(job_id, f"{len(novos)} arquivo(s) detectado(s) na pasta de destino.")
                    arquivos_baixados = [f.name for f in novos]
                if job.get("usuario_confirmou_download"):
                    break
        else:
            _elog(job_id, f"{len(links)} declaração(ões) encontrada(s). Baixando...")
            for i, link in enumerate(links):
                try:
                    async with page.expect_download(timeout=30000) as dl_info:
                        await link.click()
                    dl = await dl_info.value
                    nome = dl.suggested_filename or f"perdcomp_{i+1}.pdf"
                    caminho = dest / nome
                    await dl.save_as(str(caminho))
                    arquivos_baixados.append(nome)
                    job["arquivos"] = arquivos_baixados[:]
                    _elog(job_id, f"[{i+1}/{len(links)}] Baixado: {nome}")
                except Exception as exc:
                    _elog(job_id, f"[{i+1}] Falha no download: {exc}")

        job["arquivos"] = arquivos_baixados
        job["status"] = "concluido" if arquivos_baixados else "sem_arquivos"
        _elog(job_id, f"Concluído. {len(arquivos_baixados)} arquivo(s) baixado(s).")
        await browser.close()

def _ecac_thread(job_id: str, periodo_ini: str, periodo_fim: str):
    asyncio.run(_ecac_download_async(job_id, periodo_ini, periodo_fim))

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

# ─── DETECÇÃO DE TIPO ─────────────────────────────────────────────────────────

def detectar_tipo(texto: str, tl: str) -> str:
    primeiras = texto[:600].lower()
    if any(x in primeiras for x in ['declaração de compensação', 'dcomp', 'declaracao de compensacao']):
        return 'Compensação'
    if any(x in primeiras for x in ['pedido de ressarcimento', 'per - ressarcimento', 'ressarcimento']):
        return 'Ressarcimento'
    if any(x in primeiras for x in ['pedido de restituição', 'pedido de restituicao', 'restituição']):
        return 'Restituição'
    if 'compensação' in tl or 'compensacao' in tl:
        return 'Compensação'
    if 'ressarcimento' in tl:
        return 'Ressarcimento'
    if 'restituição' in tl or 'restituicao' in tl:
        return 'Restituição'
    return 'PER/DCOMP'

def detectar_tributo(texto: str) -> str | None:
    padroes = [
        (r'PIS[/\s]Pasep', 'PIS'), (r'\bPIS\b', 'PIS'),
        (r'COFINS\s+N[ãa]o[- ]Cumulativ', 'COFINS'), (r'\bCOFINS\b', 'COFINS'),
        (r'\bCSLL\b', 'CSLL'), (r'\bIRPJ\b', 'IRPJ'), (r'\bIRRF\b', 'IRRF'),
        (r'\bCSRF\b', 'CSRF'), (r'\bIPI\b', 'IPI'), (r'\bINSS\b', 'INSS'),
    ]
    for pat, nome in padroes:
        if re.search(pat, texto, re.IGNORECASE):
            return nome
    return None

# ─── EXTRAÇÃO DE VALORES POR TIPO ────────────────────────────────────────────

_VAL = r'([\d]{1,3}(?:\.[\d]{3})*,\d{2})'

def _val(texto: str, labels: list) -> float:
    for label in labels:
        m = re.search(label + r'[\s\t:]*' + _VAL, texto, re.IGNORECASE)
        if m:
            return parse_valor(m.group(1))
    return 0.0

def extrair_referencia_per(texto: str) -> str | None:
    """Extrai o número do PER referenciado dentro de uma DCOMP."""
    return primeiro_match(texto, [
        r'(?:N[úu]mero do )?PER[:\s]+(\d{5}\.\d{6}/\d{4}-\d{2})',
        r'Pedido de Ressarcimento[:\s]+(\d{5}\.\d{6}/\d{4}-\d{2})',
        r'Processo do Cr[eé]dito[:\s]+(\d{5}\.\d{6}/\d{4}-\d{2})',
        r'N[úu]mero do Cr[eé]dito[:\s]+(\d{5}\.\d{6}/\d{4}-\d{2})',
        r'Cr[eé]dito[^\n]{0,80}N[úu]mero[:\s]+(\d{5}\.\d{6}/\d{4}-\d{2})',
    ])

def extrair_competencia_credito(texto: str) -> str | None:
    """Extrai a competência do CRÉDITO dentro de uma DCOMP (pode ser diferente do débito)."""
    return primeiro_match(texto, [
        r'Per[íi]odo de Apura[çc][aã]o do Cr[eé]dito[:\s]+(\d{2}/\d{4})',
        r'Compet[eê]ncia do Cr[eé]dito[:\s]+(\d{2}/\d{4})',
        r'Per[íi]odo do Cr[eé]dito[:\s]+(\d{2}/\d{4})',
        r'Per[íi]odo de Apura[çc][aã]o[:\s]+(\d{2}/\d{4})',
        r'Per[íi]odo[:\s]+(\d{2}/\d{4})',
        r'Compet[eê]ncia[:\s]+(\d{2}/\d{4})',
        r'(\d{2}/\d{4})',
    ])

def extrair_dcomp(texto: str, tl: str) -> dict:
    valor_credito = _val(texto, [
        r'Valor do Cr[eé]dito Dispon[íi]vel', r'Valor do Cr[eé]dito Original',
        r'Valor Total do Cr[eé]dito', r'Valor do Cr[eé]dito',
        r'Cr[eé]dito Dispon[íi]vel',
    ])
    valor_compensado = _val(texto, [
        r'Valor Compensado', r'Valor da Compensa[çc][aã]o',
        r'Valor do D[eé]bito Compensado', r'Valor Objeto da Compensa[çc][aã]o',
        r'Valor do D[eé]bito',
    ])
    saldo = _val(texto, [
        r'Saldo do Cr[eé]dito ap[oó]s', r'Saldo Remanescente',
        r'Saldo a Compensar', r'Saldo Dispon[íi]vel',
    ])
    if valor_compensado == 0 and valor_credito > 0 and saldo > 0:
        valor_compensado = max(0.0, valor_credito - saldo)
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
        r'Valor do Ressarcimento', r'Valor Solicitado',
        r'Valor do Cr[eé]dito a Ressarcir', r'Valor do Cr[eé]dito', r'Total do Cr[eé]dito',
    ])
    valor_ressarcido = _val(texto, [r'Valor Ressarcido', r'Valor Pago', r'Valor Deferido'])
    saldo = _val(texto, [r'Saldo Remanescente', r'Saldo a Ressarcir', r'Saldo Dispon[íi]vel'])
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
        r'Valor da Restituição', r'Valor da Restituicao',
        r'Valor Solicitado', r'Valor do Cr[eé]dito', r'Valor a Restituir',
    ])
    valor_ressarcido = _val(texto, [r'Valor Restitu[íi]do', r'Valor Pago', r'Valor Deferido'])
    saldo = _val(texto, [r'Saldo Remanescente', r'Saldo a Restituir'])
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
        r'N[úu]mero do Processo[:\s]+(\d{5}\.\d{6}/\d{4}-\d{2})',
        r'Processo[:\s]+(\d{5}\.\d{6}/\d{4}-\d{2})',
        r'N[úu]mero do PER/DCOMP[:\s]+([A-Z0-9\-\/\.]{5,40})',
        r'N[úu]mero[:\s]+([A-Z0-9\-\/\.]{8,40})',
    ])

def extrair_data_tx(texto: str) -> str | None:
    return primeiro_match(texto, [
        r'Data de Transmiss[aã]o[:\s]+(\d{2}/\d{2}/\d{4})',
        r'Transmitido em[:\s]+(\d{2}/\d{2}/\d{4})',
        r'Data de Envio[:\s]+(\d{2}/\d{2}/\d{4})',
        r'Data/Hora[:\s]+(\d{2}/\d{2}/\d{4})',
    ])

def extrair_situacao(tl: str) -> str | None:
    for s in ['Homologado', 'Não Homologado', 'Deferido', 'Indeferido',
              'Cancelado', 'Em Análise', 'Em análise', 'Ativo',
              'Pendente', 'Em processamento']:
        if s.lower() in tl:
            return s
    return None

# ─── MONTAGEM DO REGISTRO ─────────────────────────────────────────────────────

def extrair_registro(texto: str, nome: str) -> dict:
    tl = texto.lower()
    tipo = detectar_tipo(texto, tl)

    if tipo == 'Compensação':
        vals = extrair_dcomp(texto, tl)
        referencia_per = extrair_referencia_per(texto)
        competencia = extrair_competencia_credito(texto)
    elif tipo == 'Ressarcimento':
        vals = extrair_per_ressarcimento(texto, tl)
        referencia_per = None
        competencia = primeiro_match(texto, [
            r'Per[íi]odo de Apura[çc][aã]o[:\s]+(\d{2}/\d{4})',
            r'Compet[eê]ncia[:\s]+(\d{2}/\d{4})',
            r'Per[íi]odo[:\s]+(\d{2}/\d{4})',
            r'(\d{2}/\d{4})',
        ])
    else:
        vals = extrair_per_restituicao(texto, tl)
        referencia_per = None
        competencia = primeiro_match(texto, [
            r'Per[íi]odo de Apura[çc][aã]o[:\s]+(\d{2}/\d{4})',
            r'(\d{2}/\d{4})',
        ])

    retificador = bool(re.search(r'retificador|retificadora', tl))
    numero_original = None
    if retificador:
        numero_original = primeiro_match(texto, [
            r'Processo Original[:\s]+(\d{5}\.\d{6}/\d{4}-\d{2})',
            r'N[úu]mero Original[:\s]+(\d{5}\.\d{6}/\d{4}-\d{2})',
            r'Retifica[çc][aã]o de[:\s]+(\d{5}\.\d{6}/\d{4}-\d{2})',
        ])

    return {
        "arquivo":          nome,
        "numero":           extrair_numero(texto),
        "numero_original":  numero_original,
        "tipo":             tipo,
        "tributo":          detectar_tributo(texto),
        "competencia":      competencia,
        "data_transmissao": extrair_data_tx(texto),
        "situacao":         extrair_situacao(tl),
        "retificador":      retificador,
        "referencia_per":   referencia_per,
        **vals,
    }

# ─── VINCULAÇÃO PER ↔ DCOMP ──────────────────────────────────────────────────

def _normalizar_num(n: str | None) -> str:
    if not n:
        return ""
    return re.sub(r'[\s\-/\.]', '', n).upper()

def vincular_pers_dcomps(registros: list) -> tuple[list, list]:
    """
    Vincula DCOMPs aos PERs de origem.
    Retificadoras substituem os originais.
    """
    pers_brutos   = [r for r in registros if r["tipo"] in ("Ressarcimento", "Restituição")]
    dcomps        = [r for r in registros if r["tipo"] == "Compensação"]

    # ── Passo 1: resolver retificadoras entre os PERs ────────────────────────
    # Chave de agrupamento: tributo + competencia (identifica o mesmo crédito)
    per_por_chave: dict[tuple, list] = {}
    for p in pers_brutos:
        chave = (p.get("tributo"), p.get("competencia"))
        per_por_chave.setdefault(chave, []).append(p)

    per_por_numero: dict[str, dict] = {}   # numero normalizado → PER efetivo
    grupos_per: list[dict] = []

    for chave, grupo in per_por_chave.items():
        originais    = [p for p in grupo if not p.get("retificador")]
        retificadoras = [p for p in grupo if p.get("retificador")]

        if retificadoras:
            efetivo = retificadoras[-1]          # mais recente
            substituidos = originais + retificadoras[:-1]
        elif originais:
            efetivo = originais[-1]
            substituidos = originais[:-1]
        else:
            continue

        efetivo["_efetivo"] = True
        for s in substituidos:
            s["_efetivo"] = False
            s["_substituido_por"] = efetivo.get("arquivo")

        grupos_per.append({
            "per_efetivo":   efetivo,
            "substituidos":  substituidos,
            "chave":         chave,
        })
        num = _normalizar_num(efetivo.get("numero"))
        if num:
            per_por_numero[num] = efetivo

    # ── Passo 2: vincular cada DCOMP ao seu PER efetivo ──────────────────────
    dcomps_por_per: dict[int, list] = {id(g["per_efetivo"]): [] for g in grupos_per}
    dcomps_nao_vinculadas: list[dict] = []

    for dcomp in dcomps:
        vinculado_a = None

        # 2a. Por número explícito referenciado no DCOMP
        ref = _normalizar_num(dcomp.get("referencia_per"))
        if ref and ref in per_por_numero:
            vinculado_a = per_por_numero[ref]

        # 2b. Por tributo + competência do crédito (mesmo crédito)
        if not vinculado_a:
            chave_dcomp = (dcomp.get("tributo"), dcomp.get("competencia"))
            for g in grupos_per:
                if g["chave"] == chave_dcomp:
                    vinculado_a = g["per_efetivo"]
                    break

        if vinculado_a:
            dcomps_por_per[id(vinculado_a)].append(dcomp)
        else:
            dcomps_nao_vinculadas.append(dcomp)

    # ── Passo 3: montar resultado com validação de saldos ───────────────────
    vinculos = []
    for g in grupos_per:
        per = g["per_efetivo"]
        linked = dcomps_por_per.get(id(per), [])

        credito      = per["valor_credito"]
        total_comp   = round(sum(d["valor_compensado"] for d in linked), 2)
        saldo_calc   = round(credito - total_comp, 2)
        saldo_decl   = per.get("saldo_remanescente", 0.0)

        # Validação de status
        if credito == 0:
            status = "SEM_VALOR"
        elif total_comp > credito * 1.005:
            status = "EXCEDIDO"
        elif saldo_decl > 0 and abs(saldo_calc - saldo_decl) > max(credito * 0.02, 1.0):
            status = "DIVERGENCIA"
        elif not linked:
            status = "SEM_DCOMPS"
        else:
            status = "OK"

        alertas_vinc = []
        if status == "EXCEDIDO":
            alertas_vinc.append(f"Total compensado (R$ {total_comp:,.2f}) supera o crédito do PER (R$ {credito:,.2f}).")
        if status == "DIVERGENCIA":
            alertas_vinc.append(f"Saldo calculado (R$ {saldo_calc:,.2f}) diverge do saldo declarado no PER (R$ {saldo_decl:,.2f}).")
        if g["substituidos"]:
            nomes = ", ".join(s["arquivo"] for s in g["substituidos"])
            alertas_vinc.append(f"Documento(s) substituído(s) por retificadora: {nomes}.")

        vinculos.append({
            "per_arquivo":          per["arquivo"],
            "per_numero":           per.get("numero"),
            "per_tributo":          per.get("tributo"),
            "per_competencia":      per.get("competencia"),
            "per_situacao":         per.get("situacao"),
            "per_data_tx":          per.get("data_transmissao"),
            "valor_credito":        credito,
            "tem_retificadora":     bool(g["substituidos"]) or per.get("retificador", False),
            "substituidos":         [{"arquivo": s["arquivo"], "numero": s.get("numero")} for s in g["substituidos"]],
            "dcomps": [
                {
                    "arquivo":          d["arquivo"],
                    "numero":           d.get("numero"),
                    "valor_compensado": d["valor_compensado"],
                    "data_transmissao": d.get("data_transmissao"),
                    "situacao":         d.get("situacao"),
                    "referencia_per":   d.get("referencia_per"),
                }
                for d in sorted(linked, key=lambda x: x.get("data_transmissao") or "")
            ],
            "total_compensado":     total_comp,
            "saldo_calculado":      saldo_calc,
            "saldo_declarado":      saldo_decl,
            "percentual_utilizado": round(total_comp / credito * 100, 1) if credito > 0 else 0,
            "status_validacao":     status,
            "alertas_vinculo":      alertas_vinc,
        })

    return vinculos, dcomps_nao_vinculadas

# ─── MOTOR DE COMPLIANCE ──────────────────────────────────────────────────────

def analisar_compliance(registros: list, vinculos: list) -> list:
    alertas = []
    hoje = date.today()

    # R1 — Dupla utilização (mesmo número de processo)
    numeros_vistos: dict[str, dict] = {}
    for r in registros:
        if not r.get("numero"):
            continue
        base = _normalizar_num(r["numero"])
        if base in numeros_vistos:
            alertas.append({
                "nivel": "alto", "tipo": "Possível Dupla Utilização",
                "descricao": f"Número '{r['numero']}' aparece em mais de um documento.",
                "arquivos": [numeros_vistos[base]["arquivo"], r["arquivo"]],
            })
        else:
            numeros_vistos[base] = r

    # R2 — Saldo excedido por vinculação
    for v in vinculos:
        if v["status_validacao"] == "EXCEDIDO":
            alertas.append({
                "nivel": "alto", "tipo": "Crédito do PER Excedido",
                "descricao": (f"PER '{v['per_arquivo']}': total compensado R$ {v['total_compensado']:,.2f} "
                              f"supera o crédito disponível R$ {v['valor_credito']:,.2f}."),
                "arquivos": [v["per_arquivo"]] + [d["arquivo"] for d in v["dcomps"]],
            })

    # R3 — Divergência de saldo
    for v in vinculos:
        if v["status_validacao"] == "DIVERGENCIA":
            alertas.append({
                "nivel": "medio", "tipo": "Divergência de Saldo",
                "descricao": (f"PER '{v['per_arquivo']}': saldo calculado R$ {v['saldo_calculado']:,.2f} "
                              f"diverge do saldo declarado R$ {v['saldo_declarado']:,.2f}."),
                "arquivos": [v["per_arquivo"]],
            })

    # R4 — DCOMPs sem PER vinculado
    for r in registros:
        if r["tipo"] == "Compensação" and not any(
            d["arquivo"] == r["arquivo"] for v in vinculos for d in v["dcomps"]
        ):
            alertas.append({
                "nivel": "medio", "tipo": "Compensação Sem PER Vinculado",
                "descricao": f"{r['arquivo']}: Não foi possível identificar o PER de origem desta compensação.",
                "arquivos": [r["arquivo"]],
            })

    # R5 — Prescrição
    for r in registros:
        comp = r.get("competencia")
        if not comp:
            continue
        try:
            partes = comp.split('/')
            if len(partes) != 2:
                continue
            mes, ano = int(partes[0]), int(partes[1])
            anos = (hoje - date(ano, mes, 1)).days / 365.25
            if anos > 5:
                alertas.append({
                    "nivel": "alto", "tipo": "Risco de Prescrição",
                    "descricao": f"{r['arquivo']}: Crédito de {comp} tem {anos:.1f} anos — possível prescrição.",
                    "arquivos": [r["arquivo"]],
                })
            elif anos > 4:
                alertas.append({
                    "nivel": "medio", "tipo": "Crédito Próximo da Prescrição",
                    "descricao": f"{r['arquivo']}: Crédito de {comp} tem {anos:.1f} anos — monitorar prazo de 5 anos.",
                    "arquivos": [r["arquivo"]],
                })
        except Exception:
            pass

    # R6 — Dados não extraídos
    for r in registros:
        falta = []
        if not r.get("tributo"):    falta.append("tributo")
        if not r.get("competencia"): falta.append("competência")
        if r["valor_credito"] == 0 and r["valor_compensado"] == 0:
            falta.append("valores monetários")
        if falta:
            alertas.append({
                "nivel": "medio", "tipo": "Dados Não Extraídos",
                "descricao": f"{r['arquivo']}: Não identificado: {', '.join(falta)}. Revisão manual necessária.",
                "arquivos": [r["arquivo"]],
            })

    # R7 — Retificadoras
    for r in registros:
        if r.get("retificador"):
            alertas.append({
                "nivel": "info", "tipo": "Documento Retificador",
                "descricao": f"{r['arquivo']}: Retificadora — substituiu documentos anteriores de mesmo tributo/competência.",
                "arquivos": [r["arquivo"]],
            })

    # R8 — Pedidos indeferidos/cancelados
    for r in registros:
        if r.get("situacao") in ("Indeferido", "Cancelado"):
            alertas.append({
                "nivel": "medio", "tipo": f"Pedido {r['situacao']}",
                "descricao": f"{r['arquivo']}: Status '{r['situacao']}' — verificar recurso ou retificação.",
                "arquivos": [r["arquivo"]],
            })

    return alertas

# ─── ROTAS ────────────────────────────────────────────────────────────────────

@bp.route('/api/perdcomp/health')
def health():
    return jsonify({"ok": True, "pdfplumber": PDF_OK,
                    "chrome_disponivel": not _chrome_bloqueado()})

# ─── ROTAS ECAC ──────────────────────────────────────────────────────────────

@bp.route('/api/perdcomp/ecac/iniciar', methods=['POST'])
def ecac_iniciar():
    data = request.json or {}
    periodo_ini = data.get("periodo_ini", "").strip()
    periodo_fim = data.get("periodo_fim", "").strip()
    if not periodo_ini or not periodo_fim:
        return jsonify({"error": "Informe o período inicial e final (MM/AAAA)."}), 400

    job_id = str(uuid.uuid4())
    _ECAC_JOBS[job_id] = {"status": "iniciando", "log": [], "arquivos": []}
    threading.Thread(target=_ecac_thread, args=(job_id, periodo_ini, periodo_fim), daemon=True).start()
    return jsonify({"job_id": job_id})

@bp.route('/api/perdcomp/ecac/status/<job_id>')
def ecac_status(job_id):
    job = _ECAC_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(job)

@bp.route('/api/perdcomp/ecac/confirmar/<job_id>', methods=['POST'])
def ecac_confirmar(job_id):
    """Usuário confirma que navegou manualmente para a lista de PER/DCOMPs."""
    job = _ECAC_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job não encontrado"}), 404
    job["usuario_confirmou"] = True
    return jsonify({"ok": True})

@bp.route('/api/perdcomp/ecac/confirmar-download/<job_id>', methods=['POST'])
def ecac_confirmar_download(job_id):
    """Usuário confirma que terminou de baixar manualmente."""
    job = _ECAC_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job não encontrado"}), 404
    job["usuario_confirmou_download"] = True
    # Escanear pasta
    dest = _ECAC_DIR / job_id
    if dest.exists():
        job["arquivos"] = [f.name for f in dest.glob("*.pdf")]
    return jsonify({"ok": True, "arquivos": job["arquivos"]})

@bp.route('/api/perdcomp/ecac/analisar/<job_id>', methods=['POST'])
def ecac_analisar(job_id):
    """Analisa os PDFs baixados do eCAC exatamente como o upload normal."""
    if not PDF_OK:
        return jsonify({"error": "pdfplumber não instalado"}), 500
    dest = _ECAC_DIR / job_id
    if not dest.exists():
        return jsonify({"error": "Pasta de downloads não encontrada"}), 404

    pdfs = list(dest.glob("*.pdf"))
    if not pdfs:
        return jsonify({"error": "Nenhum PDF na pasta de downloads"}), 400

    registros, erros = [], []
    for pdf in pdfs:
        try:
            texto = extrair_texto(pdf.read_bytes())
            if not texto.strip():
                erros.append({"arquivo": pdf.name, "erro": "PDF sem texto extraível"})
                continue
            registros.append(extrair_registro(texto, pdf.name))
        except Exception as exc:
            erros.append({"arquivo": pdf.name, "erro": str(exc)[:200]})

    vinculos, dcomps_nao_vinculadas = vincular_pers_dcomps(registros)
    alertas = analisar_compliance(registros, vinculos)

    total_credito    = sum(r["valor_credito"]      for r in registros)
    total_compensado = sum(r["valor_compensado"]   for r in registros)
    total_ressarcido = sum(r["valor_ressarcido"]   for r in registros)
    saldo_total      = sum(r["saldo_remanescente"] for r in registros)

    dist_tributos: dict[str, float] = {}
    dist_tipos:    dict[str, int]   = {}
    for r in registros:
        t  = r["tributo"] or "Não identificado"
        tp = r["tipo"]    or "Não identificado"
        val = r["valor_compensado"] if r["tipo"] == "Compensação" else r["valor_credito"]
        dist_tributos[t]  = round(dist_tributos.get(t, 0) + val, 2)
        dist_tipos[tp]    = dist_tipos.get(tp, 0) + 1

    return jsonify({
        "registros": registros, "vinculos": vinculos,
        "dcomps_nao_vinculadas": [d["arquivo"] for d in dcomps_nao_vinculadas],
        "alertas": alertas, "erros": erros,
        "sumario": {
            "total_arquivos": len(registros), "total_credito": round(total_credito, 2),
            "total_compensado": round(total_compensado, 2),
            "total_ressarcido": round(total_ressarcido, 2),
            "saldo_disponivel": round(saldo_total, 2),
            "alertas_alto":   sum(1 for a in alertas if a["nivel"] == "alto"),
            "alertas_medio":  sum(1 for a in alertas if a["nivel"] == "medio"),
            "alertas_info":   sum(1 for a in alertas if a["nivel"] == "info"),
            "dist_tributos": dist_tributos, "dist_tipos": dist_tipos,
            "vinculos_ok":        sum(1 for v in vinculos if v["status_validacao"] == "OK"),
            "vinculos_excedidos": sum(1 for v in vinculos if v["status_validacao"] == "EXCEDIDO"),
            "vinculos_diverg":    sum(1 for v in vinculos if v["status_validacao"] == "DIVERGENCIA"),
            "total_pers":  sum(1 for r in registros if r["tipo"] in ("Ressarcimento","Restituição")),
            "total_dcomps":sum(1 for r in registros if r["tipo"] == "Compensação"),
        }
    })

@bp.route('/api/perdcomp/debug', methods=['POST'])
def debug():
    if not PDF_OK:
        return jsonify({"error": "pdfplumber não instalado"}), 500
    files = request.files.getlist('files')
    resultado = []
    for f in files:
        if not f.filename:
            continue
        texto = extrair_texto(f.read())
        resultado.append({
            "arquivo":              f.filename,
            "chars":                len(texto),
            "texto":                texto[:3000],
            "tipo_detectado":       detectar_tipo(texto, texto.lower()),
            "tributo_detectado":    detectar_tributo(texto),
            "numero_detectado":     extrair_numero(texto),
            "competencia_detectada":extrair_competencia_credito(texto),
            "referencia_per":       extrair_referencia_per(texto),
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
                              "PDF sem texto extraível — pode ser escaneado"})
                continue
            registros.append(extrair_registro(texto, f.filename))
        except Exception as e:
            erros.append({"arquivo": f.filename, "erro": str(e)[:300]})

    if not registros and erros:
        return jsonify({"error": "Nenhum arquivo processado", "detalhes": erros}), 400

    vinculos, dcomps_nao_vinculadas = vincular_pers_dcomps(registros)
    alertas = analisar_compliance(registros, vinculos)

    total_credito    = sum(r["valor_credito"]      for r in registros)
    total_compensado = sum(r["valor_compensado"]   for r in registros)
    total_ressarcido = sum(r["valor_ressarcido"]   for r in registros)
    saldo_total      = sum(r["saldo_remanescente"] for r in registros)

    dist_tributos: dict[str, float] = {}
    dist_tipos:    dict[str, int]   = {}
    for r in registros:
        t  = r["tributo"] or "Não identificado"
        tp = r["tipo"]    or "Não identificado"
        val = r["valor_compensado"] if r["tipo"] == "Compensação" else r["valor_credito"]
        dist_tributos[t]  = round(dist_tributos.get(t, 0) + val, 2)
        dist_tipos[tp]    = dist_tipos.get(tp, 0) + 1

    return jsonify({
        "registros":              registros,
        "vinculos":               vinculos,
        "dcomps_nao_vinculadas":  [d["arquivo"] for d in dcomps_nao_vinculadas],
        "alertas":                alertas,
        "erros":                  erros,
        "sumario": {
            "total_arquivos":    len(registros),
            "total_pers":        sum(1 for r in registros if r["tipo"] in ("Ressarcimento", "Restituição")),
            "total_dcomps":      sum(1 for r in registros if r["tipo"] == "Compensação"),
            "total_credito":     round(total_credito, 2),
            "total_compensado":  round(total_compensado, 2),
            "total_ressarcido":  round(total_ressarcido, 2),
            "saldo_disponivel":  round(saldo_total, 2),
            "alertas_alto":      sum(1 for a in alertas if a["nivel"] == "alto"),
            "alertas_medio":     sum(1 for a in alertas if a["nivel"] == "medio"),
            "alertas_info":      sum(1 for a in alertas if a["nivel"] == "info"),
            "dist_tributos":     dist_tributos,
            "dist_tipos":        dist_tipos,
            "vinculos_ok":       sum(1 for v in vinculos if v["status_validacao"] == "OK"),
            "vinculos_excedidos":sum(1 for v in vinculos if v["status_validacao"] == "EXCEDIDO"),
            "vinculos_diverg":   sum(1 for v in vinculos if v["status_validacao"] == "DIVERGENCIA"),
        }
    })
