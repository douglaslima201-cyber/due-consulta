"""
DUE Consulta Backend - Servidor Flask com automação Playwright
Execução: python main.py
"""

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

app = Flask(__name__)
CORS(app)

_BASE_DIR = Path(__file__).parent
DB_PATH = str(_BASE_DIR / "consultas.db")
UPLOAD_DIR = _BASE_DIR / "uploads"
RESULTS_DIR = _BASE_DIR / "results"
UPLOAD_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)

ANTICAPTCHA_KEY = "6d73ae3890ea23b5d54c6240355586c2"
PORTAL_URL = "https://portalunico.siscomex.gov.br/due/x/#/consulta/consulta-filtro?perfil=publico"

# ─── Banco de dados ─────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            status TEXT,
            total INTEGER,
            processed INTEGER,
            created_at TEXT,
            finished_at TEXT,
            input_file TEXT,
            output_file TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS resultados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            chave_nfe TEXT,
            status_nfe TEXT,
            numero_due TEXT,
            data_due TEXT,
            status_due TEXT,
            observacao TEXT,
            consultado_em TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            nivel TEXT,
            mensagem TEXT,
            criado_em TEXT
        )
    """)
    conn.commit()
    conn.close()

def log(job_id, nivel, msg):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO logs (job_id, nivel, mensagem, criado_em) VALUES (?, ?, ?, ?)",
        (job_id, nivel, msg, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    print(f"[{nivel}] [{job_id[:8]}] {msg}")

def update_job(job_id, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [job_id]
    conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", vals)
    conn.commit()
    conn.close()

def salvar_resultado(job_id, chave, status_nfe, numero_due="", data_due="", status_due="", obs=""):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO resultados 
           (job_id, chave_nfe, status_nfe, numero_due, data_due, status_due, observacao, consultado_em)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (job_id, chave, status_nfe, numero_due, data_due, status_due, obs, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

# ─── Validação de chave NF-e ─────────────────────────────────────────────────

def validar_chave(chave: str) -> bool:
    chave = re.sub(r'\D', '', str(chave))
    return len(chave) == 44 and chave.isdigit()

def normalizar_chave(chave: str) -> str:
    return re.sub(r'\D', '', str(chave))

# ─── Resolução de CAPTCHA via 2captcha/anticaptcha ──────────────────────────

async def resolver_captcha_anticaptcha(site_key: str, page_url: str, captcha_type: str = "hcaptcha") -> str | None:
    """Resolve hCaptcha ou reCAPTCHA via anti-captcha.com API."""
    task_type = "HCaptchaTaskProxyless" if captcha_type == "hcaptcha" else "NoCaptchaTaskProxyless"
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "clientKey": ANTICAPTCHA_KEY,
                "task": {
                    "type": task_type,
                    "websiteURL": page_url,
                    "websiteKey": site_key
                }
            }
            async with session.post("https://api.anti-captcha.com/createTask", json=payload) as resp:
                data = await resp.json()
                if data.get("errorId") != 0:
                    print(f"Anti-captcha createTask erro: {data.get('errorDescription')}")
                    return None
                task_id = data["taskId"]

            for _ in range(60):
                await asyncio.sleep(3)
                async with session.post("https://api.anti-captcha.com/getTaskResult",
                                        json={"clientKey": ANTICAPTCHA_KEY, "taskId": task_id}) as resp:
                    result = await resp.json()
                    if result.get("status") == "ready":
                        return result["solution"]["gRecaptchaResponse"]
                    if result.get("errorId") != 0:
                        print(f"Anti-captcha getTaskResult erro: {result.get('errorDescription')}")
                        return None
    except Exception as e:
        print(f"Erro anticaptcha: {e}")
    return None


async def _obter_sitekey(page) -> str | None:
    """Extrai data-sitekey do reCAPTCHA buscando na página principal, frames e objeto JS interno."""
    # 1. Página principal (timeout curto — não aguardar se não existir)
    try:
        sitekey = await page.locator('[data-sitekey]').first.get_attribute("data-sitekey", timeout=2000)
        if sitekey:
            return sitekey
    except Exception:
        pass

    # 2. Frames carregados (reCAPTCHA vive dentro de iframes)
    for frame in page.frames:
        if not frame.url or frame.url == "about:blank":
            continue
        try:
            sitekey = await frame.locator('[data-sitekey]').first.get_attribute("data-sitekey", timeout=1000)
            if sitekey:
                return sitekey
        except Exception:
            continue

    # 3. FrameLocator para iframes não acessíveis diretamente
    for iframe_sel in [
        'iframe[src*="recaptcha"]',
        'iframe[src*="captcha"]',
        'iframe[title*="reCAPTCHA"]',
        'iframe[title*="recaptcha"]',
    ]:
        try:
            sitekey = await page.frame_locator(iframe_sel).locator('[data-sitekey]').first.get_attribute(
                "data-sitekey", timeout=1000
            )
            if sitekey:
                return sitekey
        except Exception:
            continue

    # 4. DOM via JavaScript (captura elementos ocultos ou renderizados por frameworks)
    try:
        sitekey = await page.evaluate(
            "() => { const el = document.querySelector('[data-sitekey]'); return el ? el.getAttribute('data-sitekey') : null; }"
        )
        if sitekey:
            return sitekey
    except Exception:
        pass

    # 5. Extrair sitekey da URL do iframe hCaptcha — o portal Siscomex usa Angular
    #    que não coloca data-sitekey no DOM; o sitekey fica no hash da URL do iframe
    #    ex: https://newassets.hcaptcha.com/.../hcaptcha.html#anchor?sitekey=UUID&...
    for frame in page.frames:
        url = frame.url or ""
        if "hcaptcha.com" in url.lower():
            match = re.search(r'[?&#]sitekey=([0-9a-f-]{20,})', url)
            if match:
                return match.group(1)

    # 6. Sitekey no src do iframe hCaptcha via DOM (antes do frame carregar)
    try:
        sitekey = await page.evaluate("""
            () => {
                for (const f of document.querySelectorAll('iframe')) {
                    const src = f.src || f.getAttribute('src') || '';
                    if (!src.includes('hcaptcha')) continue;
                    const m = src.match(/[?&#]sitekey=([0-9a-f-]{20,})/);
                    if (m) return m[1];
                }
                return null;
            }
        """)
        if sitekey:
            return sitekey
    except Exception:
        pass

    # 7. Objeto interno ___grecaptcha_cfg (reCAPTCHA invisível/v3)
    try:
        sitekey = await page.evaluate("""
            () => {
                try {
                    const cfg = window.___grecaptcha_cfg;
                    if (!cfg || !cfg.clients) return null;
                    for (const client of Object.values(cfg.clients)) {
                        for (const val of Object.values(client)) {
                            if (!val || typeof val !== 'object') continue;
                            if (val.sitekey) return val.sitekey;
                            for (const v2 of Object.values(val)) {
                                if (v2 && typeof v2 === 'object' && v2.sitekey) return v2.sitekey;
                            }
                        }
                    }
                } catch(e) {}
                return null;
            }
        """)
        if sitekey:
            return sitekey
    except Exception:
        pass

    # 8. Sitekey em tags <script> ou atributos da página (fallback final)
    try:
        sitekey = await page.evaluate(r"""
            () => {
                for (const s of document.querySelectorAll('script, [sitekey], [data-sitekey]')) {
                    const attr = s.getAttribute('sitekey') || s.getAttribute('data-sitekey');
                    if (attr && attr.length > 10) return attr;
                    const m = (s.textContent || '').match(/"sitekey"\s*:\s*"([^"]{20,})"/);
                    if (m) return m[1];
                }
                return null;
            }
        """)
        if sitekey:
            return sitekey
    except Exception:
        pass

    return None


async def _injetar_token_captcha(page, token: str, captcha_type: str = "hcaptcha") -> bool:
    """Injeta token de hCaptcha ou reCAPTCHA e aciona callback do formulário."""
    try:
        if captcha_type == "hcaptcha":
            await page.evaluate("""
                (token) => {
                    // hCaptcha: usar native setter para compatibilidade com Angular/React
                    const hc = document.querySelector('textarea[name="h-captcha-response"]');
                    if (hc) {
                        try {
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLTextAreaElement.prototype, 'value'
                            ).set;
                            setter.call(hc, token);
                        } catch(e) {
                            hc.value = token;
                        }
                        hc.dispatchEvent(new Event('input', { bubbles: true }));
                        hc.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    // Fallback g-recaptcha-response (alguns portais leem os dois)
                    const gc = document.getElementById('g-recaptcha-response');
                    if (gc) { gc.value = token; }
                    // Acionar data-callback se definido
                    try {
                        const el = document.querySelector('[data-callback]');
                        if (el) {
                            const fn = window[el.getAttribute('data-callback')];
                            if (typeof fn === 'function') fn(token);
                        }
                    } catch(e) {}
                }
            """, token)
        else:
            await page.evaluate("""
                (token) => {
                    const resp = document.getElementById('g-recaptcha-response');
                    if (resp) {
                        resp.style.display = 'block';
                        resp.value = token;
                        resp.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    try {
                        const cfg = window.___grecaptcha_cfg;
                        if (cfg && cfg.clients) {
                            Object.values(cfg.clients).forEach(client => {
                                const entry = Object.values(client).find(
                                    v => v && typeof v === 'object' && typeof v.callback === 'function'
                                );
                                if (entry) entry.callback(token);
                            });
                        }
                    } catch(e) {}
                    try {
                        const el = document.querySelector('[data-callback]');
                        if (el) {
                            const fn = window[el.getAttribute('data-callback')];
                            if (typeof fn === 'function') fn(token);
                        }
                    } catch(e) {}
                }
            """, token)
        return True
    except Exception as e:
        print(f"Erro ao injetar token captcha: {e}")
        return False


async def verificar_e_resolver_captcha(page, job_id: str) -> bool:
    """
    Aguarda captcha inicializar (até 10s), detecta tipo (hCaptcha/reCAPTCHA),
    extrai sitekey, resolve via anti-captcha e injeta token.
    Retorna True se não há captcha ou se foi resolvido. Retorna False se falhou.
    """
    sitekey = None
    tem_indicador = False
    captcha_type = "hcaptcha"  # portal Siscomex usa hCaptcha

    for tentativa in range(10):
        # Detectar tipo pelo URL dos frames
        for frame in page.frames:
            url = (frame.url or "").lower()
            if "hcaptcha.com" in url:
                captcha_type = "hcaptcha"
                tem_indicador = True
                break
            if "recaptcha" in url:
                captcha_type = "recaptcha"
                tem_indicador = True
                break

        # Checar elementos DOM se frames não indicaram
        if not tem_indicador:
            try:
                count = await page.locator(
                    '.h-captcha, .g-recaptcha, [data-sitekey], '
                    'iframe[src*="hcaptcha"], iframe[src*="recaptcha"]'
                ).count()
                if count > 0:
                    tem_indicador = True
            except Exception:
                pass

        if tem_indicador:
            sitekey = await _obter_sitekey(page)
            if sitekey:
                break
            log(job_id, "INFO", f"CAPTCHA carregando... ({tentativa + 1}/10)")
            await asyncio.sleep(1)
        else:
            await asyncio.sleep(1)

    if not tem_indicador and not sitekey:
        return True  # Sem CAPTCHA na página

    if not sitekey:
        log(job_id, "ERROR", "CAPTCHA detectado mas sitekey inacessível após 10s")
        return False

    log(job_id, "INFO", f"CAPTCHA tipo={captcha_type} sitekey={sitekey[:20]}... — resolvendo")
    token = await resolver_captcha_anticaptcha(sitekey, PORTAL_URL, captcha_type)
    if not token:
        log(job_id, "ERROR", "Anti-captcha não retornou token")
        return False

    sucesso = await _injetar_token_captcha(page, token, captcha_type)
    if not sucesso:
        log(job_id, "WARN", "Falha ao injetar token CAPTCHA")
        return False

    log(job_id, "INFO", "Token CAPTCHA injetado — aguardando callback")
    await asyncio.sleep(2)
    return True

# ─── Automação Playwright ─────────────────────────────────────────────────────

async def consultar_chave(page, chave: str, job_id: str) -> dict:
    """Consulta uma chave NF-e no portal Siscomex"""
    resultado = {
        "chave": chave,
        "status_nfe": "Erro",
        "numero_due": "",
        "data_due": "",
        "status_due": "",
        "obs": ""
    }

    try:
        log(job_id, "INFO", f"Consultando chave: {chave[:10]}...{chave[-6:]}")

        # Navegar para o portal
        await page.goto(PORTAL_URL, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)

        # Selecionar tipo "NF-e" — o portal inicia em DU-E, precisa trocar o radio
        nfe_clicado = False
        for sel in ['label:has-text("NF-e")', 'label:has-text("NF-E")',
                    'input[value="NFE"]', 'input[value="NF-e"]', 'input[value="NF_E"]']:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.click()
                    nfe_clicado = True
                    log(job_id, "INFO", f"Radio NF-e clicado via: {sel}")
                    await asyncio.sleep(1)
                    break
            except Exception:
                continue
        if not nfe_clicado:
            log(job_id, "WARN", "Radio NF-e não encontrado — tentando continuar no tipo atual")

        # Localizar campo de entrada da chave NF-e
        # IMPORTANTE: excluir o input da barra de busca do navbar usando seletores específicos
        # O campo correto fica no conteúdo principal da página, não no nav
        input_selectors = [
            'input[placeholder*="chave" i]',
            'input[placeholder*="NF-e" i]',
            'input[placeholder*="acesso" i]',
            'input[maxlength="44"]',
            'input[id*="chave" i]',
            'input[name*="chave" i]',
            # Fallback: input de texto dentro do formulário/conteúdo principal (excluindo navbar)
            'main input[type="text"]:not([placeholder*="Buscar" i])',
            'section input[type="text"]:not([placeholder*="Buscar" i])',
            'form input[type="text"]',
            '.filtros input[type="text"]',
        ]

        campo = None
        for sel in input_selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    campo = el
                    log(job_id, "INFO", f"Campo chave encontrado: {sel}")
                    break
            except Exception:
                continue

        if not campo:
            resultado["obs"] = "Campo de entrada não localizado"
            resultado["status_nfe"] = "Erro Layout"
            return resultado

        await campo.click()
        await campo.fill("")
        await campo.type(chave, delay=30)
        await asyncio.sleep(1)

        # Verificar e resolver CAPTCHA se presente
        captcha_ok = await verificar_e_resolver_captcha(page, job_id)
        if not captcha_ok:
            resultado["obs"] = "Falha ao resolver CAPTCHA"
            resultado["status_nfe"] = "Erro CAPTCHA"
            return resultado

        # Submeter consulta
        submit_selectors = [
            'button[type="submit"]',
            'button:has-text("Consultar")',
            'button:has-text("Pesquisar")',
            'button:has-text("Buscar")',
            'input[type="submit"]',
        ]
        submitted = False
        for sel in submit_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    submitted = True
                    break
            except Exception:
                continue

        if not submitted:
            await campo.press("Enter")

        # Aguardar resultado
        await asyncio.sleep(3)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeout:
            pass

        # Extrair resultado da página
        page_content = await page.content()
        page_text = await page.evaluate("document.body.innerText")

        # Analisar resultado
        texto_lower = page_text.lower()

        if "averbada" in texto_lower and "não averbada" not in texto_lower:
            resultado["status_nfe"] = "Averbada"

            # Tentar extrair número da DUE
            due_patterns = [
                r'DUE[:\s]+(\d{2}/\d+/\d{4}-\d)',
                r'(\d{2}/\d{7,}/\d{4}-\d)',
                r'Número da DUE[:\s]+([^\s\n]+)',
                r'DUE\s*N[°º]?\s*[:\s]+([^\s\n]+)',
            ]
            for pattern in due_patterns:
                match = re.search(pattern, page_text, re.IGNORECASE)
                if match:
                    resultado["numero_due"] = match.group(1).strip()
                    break

            # Tentar extrair data
            date_patterns = [
                r'(\d{2}/\d{2}/\d{4})',
                r'(\d{4}-\d{2}-\d{2})',
            ]
            for pattern in date_patterns:
                match = re.search(pattern, page_text)
                if match:
                    resultado["data_due"] = match.group(1)
                    break

            # Status da DUE
            if "desembaraçada" in texto_lower or "desembaracada" in texto_lower:
                resultado["status_due"] = "Desembaraçada"
            elif "registrada" in texto_lower:
                resultado["status_due"] = "Registrada"
            elif "ativa" in texto_lower:
                resultado["status_due"] = "Ativa"

        elif "não averbada" in texto_lower or "nao averbada" in texto_lower:
            resultado["status_nfe"] = "Não Averbada"

        elif "não encontrada" in texto_lower or "nao encontrada" in texto_lower \
                or "nenhum resultado" in texto_lower or "sem resultado" in texto_lower:
            resultado["status_nfe"] = "Não Encontrada"

        elif any(kw in texto_lower for kw in [
            "pucx-er0204", "desafio do captcha", "você deve responder", "responder o desafio"
        ]):
            resultado["status_nfe"] = "Erro CAPTCHA"
            resultado["obs"] = "Portal retornou erro de CAPTCHA — será retentado"
            log(job_id, "WARN", "Portal exigiu CAPTCHA na resposta — marcando para retry")

        else:
            screenshot_path = f"debug_{chave[:10]}_{int(time.time())}.png"
            try:
                await page.screenshot(path=screenshot_path)
                resultado["obs"] = f"Resultado indefinido — screenshot: {screenshot_path}"
            except Exception:
                resultado["obs"] = "Resultado indefinido"
            resultado["status_nfe"] = "Indefinido"
            log(job_id, "WARN", f"Resultado indefinido para {chave[:10]}... — screenshot: {screenshot_path}")

    except PlaywrightTimeout:
        resultado["status_nfe"] = "Timeout"
        resultado["obs"] = "Timeout na consulta"
        log(job_id, "ERROR", f"Timeout consultando {chave[:10]}...")

    except Exception as e:
        resultado["status_nfe"] = "Erro"
        resultado["obs"] = str(e)[:200]
        log(job_id, "ERROR", f"Erro ao consultar {chave[:10]}...: {e}")

    return resultado

# ─── Worker assíncrono — nova arquitetura: captcha uma vez + API direta ──────

API_BASE = "https://portalunico.siscomex.gov.br/due"
HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://portalunico.siscomex.gov.br/due/x/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/plain, */*",
}


async def obter_sessao_com_captcha(job_id: str) -> tuple[dict, str] | None:
    """
    Abre Chrome, aguarda o usuário resolver o hCaptcha uma única vez.
    Retorna (cookies, csrf_token) prontos para chamadas HTTP diretas.
    """
    import aiohttp as _aio

    csrf_holder = {"token": None}
    captcha_ok = asyncio.Event()

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=False)
            log(job_id, "INFO", "Chrome aberto — resolva o CAPTCHA para iniciar")
        except Exception:
            browser = await p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
            log(job_id, "INFO", "Chromium aberto — resolva o CAPTCHA para iniciar")

        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=HEADERS_BASE["User-Agent"],
        )
        page = await context.new_page()
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['pt-BR', 'pt', 'en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)

        async def rastrear_resposta(response):
            token = response.headers.get("x-csrf-token")
            if token:
                csrf_holder["token"] = token
            # Captcha validado quando portal/proxy/captcha retorna 200/204
            if "portal/proxy/captcha" in response.url and response.status in (200, 204):
                log(job_id, "INFO", "CAPTCHA validado! Iniciando consultas via API...")
                captcha_ok.set()

        page.on("response", rastrear_resposta)
        await page.goto(PORTAL_URL, wait_until="networkidle", timeout=30000)

        log(job_id, "INFO", "Aguardando resolução do CAPTCHA (até 5 minutos)...")
        try:
            await asyncio.wait_for(captcha_ok.wait(), timeout=300)
        except asyncio.TimeoutError:
            log(job_id, "ERROR", "Timeout: CAPTCHA não foi resolvido em 5 minutos")
            await browser.close()
            return None

        # Capturar cookies e CSRF token da sessão do browser
        cookies_list = await context.cookies()
        cookies = {c["name"]: c["value"] for c in cookies_list}
        csrf = csrf_holder["token"]

        # Deixar o browser aberto por 3s para o portal registrar a sessão
        await asyncio.sleep(3)
        await browser.close()

    return cookies, csrf


async def consultar_via_api(
    session,  # aiohttp.ClientSession
    chave: str,
    csrf_token: str,
    job_id: str,
) -> tuple[dict, str]:
    """
    Consulta uma chave NF-e diretamente via HTTP (sem browser).
    Retorna (resultado_dict, novo_csrf_token).
    """
    import aiohttp as _aio

    resultado = {
        "chave": chave,
        "status_nfe": "Erro",
        "numero_due": "",
        "data_due": "",
        "status_due": "",
        "obs": "",
    }

    headers = {**HEADERS_BASE, "X-CSRF-Token": csrf_token}

    try:
        # Parâmetro correto: chaveNfe (descoberto via análise do JS Angular do portal)
        url = f"{API_BASE}/api/due/listar-due-consulta"
        async with session.get(url, headers=headers, params={"chaveNfe": chave}) as resp:
            novo_csrf = resp.headers.get("x-csrf-token", csrf_token)
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = await resp.text()

            if resp.status == 403:
                msg = body.get("message", "") if isinstance(body, dict) else str(body)
                if "CAPTCHA" in msg.upper():
                    resultado["status_nfe"] = "Erro CAPTCHA"
                    resultado["obs"] = "Sessão expirou — CAPTCHA precisa ser renovado"
                else:
                    resultado["status_nfe"] = "Erro"
                    resultado["obs"] = f"HTTP 403: {msg[:150]}"
                return resultado, novo_csrf

            if resp.status != 200:
                detalhe = str(body)[:300] if body else ""
                log(job_id, "WARN", f"HTTP {resp.status} para {chave[:10]}...: {detalhe}")
                resultado["obs"] = f"HTTP {resp.status}: {detalhe}"
                return resultado, novo_csrf

            # A resposta pode ter envelope listaDueCover ou ser lista direta
            if isinstance(body, dict):
                itens = body.get("listaDueCover") or body.get("listaDue") or [body]
            elif isinstance(body, list):
                itens = body
            else:
                itens = []

            log(job_id, "INFO", f"Resposta API ({len(itens)} item(ns)): {json.dumps(body, ensure_ascii=False)[:400]}")

            if not itens:
                resultado["status_nfe"] = "Não Encontrada"
                return resultado, novo_csrf

            item = itens[0]

            # Campos confirmados pelo JS: nrDue, dataRegistro, situacao
            def campo(*nomes):
                for n in nomes:
                    v = item.get(n)
                    if v is not None:
                        return str(v)
                return ""

            numero_due = campo("nrDue", "numeroDue", "numero")
            data_due   = campo("dataRegistro", "dataInclusao", "dataDue", "data")
            status_due = campo("situacao", "statusDue", "status")

            resultado["numero_due"] = numero_due
            resultado["data_due"]   = data_due
            resultado["status_due"] = status_due

            # Determinar averbação: se tem nrDue e situação indica exportação concluída
            situacao_lower = status_due.lower()
            if numero_due and ("averbad" in situacao_lower or "desembarac" in situacao_lower
                               or "encerrad" in situacao_lower or "registrad" in situacao_lower):
                resultado["status_nfe"] = "Averbada"
            elif "nao averbad" in situacao_lower or "não averbad" in situacao_lower:
                resultado["status_nfe"] = "Não Averbada"
            elif numero_due:
                resultado["status_nfe"] = "Averbada"
            else:
                resultado["status_nfe"] = "Não Encontrada"

    except Exception as e:
        resultado["obs"] = str(e)[:200]
        log(job_id, "ERROR", f"Erro na API para {chave[:10]}...: {e}")
        return resultado, csrf_token

    return resultado, novo_csrf


async def processar_job_async(job_id: str, chaves: list[str]):
    import aiohttp

    log(job_id, "INFO", f"Iniciando processamento de {len(chaves)} chaves")
    update_job(job_id, status="running", total=len(chaves), processed=0)

    # Fase 1 — abrir Chrome, usuário resolve captcha UMA vez
    sessao = await obter_sessao_com_captcha(job_id)
    if not sessao:
        update_job(job_id, status="error", finished_at=datetime.now().isoformat())
        return

    cookies, csrf_token = sessao
    log(job_id, "INFO", "Sessão obtida — processando chaves via API direta")

    # Fase 2 — consultar todas as chaves sem abrir browser
    captcha_expirou = False
    async with aiohttp.ClientSession(cookies=cookies) as http:
        for idx, chave in enumerate(chaves):
            # Verificar cancelamento
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            conn.close()
            if row and row[0] == "cancelled":
                log(job_id, "INFO", "Job cancelado pelo usuário")
                break

            log(job_id, "INFO", f"Consultando {idx+1}/{len(chaves)}: {chave[:10]}...{chave[-6:]}")
            resultado, csrf_token = await consultar_via_api(http, chave, csrf_token, job_id)

            if resultado["status_nfe"] == "Erro CAPTCHA" and not captcha_expirou:
                # Sessão expirou — tentar renovar captcha uma vez
                log(job_id, "WARN", "Sessão expirou — renovando CAPTCHA...")
                captcha_expirou = True
                nova_sessao = await obter_sessao_com_captcha(job_id)
                if nova_sessao:
                    cookies, csrf_token = nova_sessao
                    # Atualizar cookies na sessão aiohttp
                    http.cookie_jar.update_cookies(cookies)
                    resultado, csrf_token = await consultar_via_api(http, chave, csrf_token, job_id)
                else:
                    log(job_id, "ERROR", "Não foi possível renovar sessão")
                    break

            salvar_resultado(
                job_id, chave,
                resultado["status_nfe"],
                resultado["numero_due"],
                resultado["data_due"],
                resultado["status_due"],
                resultado["obs"],
            )
            update_job(job_id, processed=idx + 1)
            await asyncio.sleep(0.2)  # delay leve entre chamadas

    output_file = gerar_relatorio(job_id)
    update_job(job_id, status="done", finished_at=datetime.now().isoformat(), output_file=str(output_file))
    log(job_id, "INFO", f"Job concluído. Relatório: {output_file}")


def processar_job_thread(job_id: str, chaves: list[str]):
    asyncio.run(processar_job_async(job_id, chaves))

# ─── Geração de relatório Excel ───────────────────────────────────────────────

def gerar_relatorio(job_id: str) -> Path:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT chave_nfe, status_nfe, numero_due, data_due, status_due, observacao, consultado_em
           FROM resultados WHERE job_id=? ORDER BY id""",
        (job_id,)
    ).fetchall()
    conn.close()

    df = pd.DataFrame(rows, columns=[
        "Chave NF-e", "Status", "Número DUE", "Data DUE",
        "Status DUE", "Observações", "Consultado em"
    ])

    output_path = RESULTS_DIR / f"resultado_{job_id[:8]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    with pd.ExcelWriter(str(output_path), engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Resultados", index=False)
        ws = writer.sheets["Resultados"]

        # Formatação
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter

        header_fill = PatternFill("solid", start_color="1B3A5C")
        header_font = Font(bold=True, color="FFFFFF", size=11)

        for col in range(1, len(df.columns) + 1):
            cell = ws.cell(row=1, column=col)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        # Colorir linhas por status
        green = PatternFill("solid", start_color="C6EFCE")
        yellow = PatternFill("solid", start_color="FFEB9C")
        red = PatternFill("solid", start_color="FFC7CE")
        gray = PatternFill("solid", start_color="D9D9D9")

        status_colors = {
            "Averbada": green,
            "Não Averbada": yellow,
            "Não Encontrada": gray,
        }

        for row_idx, row in enumerate(df.itertuples(), start=2):
            status = row.Status
            fill = status_colors.get(status, red)
            for col in range(1, len(df.columns) + 1):
                ws.cell(row=row_idx, column=col).fill = fill

        # Largura das colunas
        col_widths = [50, 18, 30, 18, 20, 40, 22]
        for i, w in enumerate(col_widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

        # Aba de resumo
        ws_sum = writer.book.create_sheet("Resumo")
        total = len(df)
        averbadas = (df["Status"] == "Averbada").sum()
        nao_averbadas = (df["Status"] == "Não Averbada").sum()
        nao_encontradas = (df["Status"] == "Não Encontrada").sum()
        erros = total - averbadas - nao_averbadas - nao_encontradas

        summary_data = [
            ["Métrica", "Quantidade", "%"],
            ["Total consultado", total, "100%"],
            ["Averbadas", averbadas, f"{averbadas/total*100:.1f}%" if total else "0%"],
            ["Não Averbadas", nao_averbadas, f"{nao_averbadas/total*100:.1f}%" if total else "0%"],
            ["Não Encontradas", nao_encontradas, f"{nao_encontradas/total*100:.1f}%" if total else "0%"],
            ["Erros", erros, f"{erros/total*100:.1f}%" if total else "0%"],
            ["", "", ""],
            ["Gerado em", datetime.now().strftime("%d/%m/%Y %H:%M:%S"), ""],
        ]
        for r_data in summary_data:
            ws_sum.append(r_data)

    return output_path

# ─── Rotas da API ─────────────────────────────────────────────────────────────

@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400

    f = request.files["file"]
    if not f.filename.endswith((".xlsx", ".csv")):
        return jsonify({"error": "Formato inválido. Use .xlsx ou .csv"}), 400

    job_id = str(uuid.uuid4())
    file_path = UPLOAD_DIR / f"{job_id}_{f.filename}"
    f.save(str(file_path))

    # Ler e validar chaves
    try:
        if str(file_path).endswith(".csv"):
            df = pd.read_csv(str(file_path), dtype=str)
        else:
            df = pd.read_excel(str(file_path), dtype=str)
    except Exception as e:
        return jsonify({"error": f"Erro ao ler arquivo: {e}"}), 400

    # Localizar coluna com chaves NF-e
    chave_col = None
    for col in df.columns:
        if any(kw in col.lower() for kw in ["chave", "nfe", "nf-e", "key", "nota"]):
            chave_col = col
            break
    if not chave_col:
        # Tentar primeira coluna com strings de 44 dígitos
        for col in df.columns:
            sample = df[col].dropna().astype(str).str.replace(r'\D', '', regex=True)
            if sample.str.len().eq(44).any():
                chave_col = col
                break

    if not chave_col:
        return jsonify({"error": "Coluna com chaves NF-e não encontrada"}), 400

    chaves_raw = df[chave_col].dropna().astype(str).tolist()
    chaves_valid = []
    chaves_invalidas = []

    for c in chaves_raw:
        norm = normalizar_chave(c)
        if validar_chave(norm):
            if norm not in chaves_valid:
                chaves_valid.append(norm)
        else:
            chaves_invalidas.append(c)

    if not chaves_valid:
        return jsonify({"error": "Nenhuma chave NF-e válida encontrada"}), 400

    # Criar job no banco
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO jobs (id, status, total, processed, created_at, input_file) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, "pending", len(chaves_valid), 0, datetime.now().isoformat(), str(file_path))
    )
    conn.commit()
    conn.close()

    # Salvar lista de chaves para o job
    chaves_path = UPLOAD_DIR / f"{job_id}_chaves.json"
    chaves_path.write_text(json.dumps(chaves_valid))

    log(job_id, "INFO", f"Upload processado: {len(chaves_valid)} válidas, {len(chaves_invalidas)} inválidas")

    return jsonify({
        "job_id": job_id,
        "total_validas": len(chaves_valid),
        "total_invalidas": len(chaves_invalidas),
        "invalidas_preview": chaves_invalidas[:10],
        "coluna_detectada": chave_col
    })

@app.route("/api/iniciar/<job_id>", methods=["POST"])
def iniciar(job_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "Job não encontrado"}), 404
    if row[0] not in ("pending",):
        return jsonify({"error": f"Job já está em status: {row[0]}"}), 400

    chaves_path = UPLOAD_DIR / f"{job_id}_chaves.json"
    if not chaves_path.exists():
        return jsonify({"error": "Arquivo de chaves não encontrado"}), 400

    chaves = json.loads(chaves_path.read_text())

    t = threading.Thread(target=processar_job_thread, args=(job_id, chaves), daemon=True)
    t.start()

    return jsonify({"message": "Processamento iniciado", "job_id": job_id})

@app.route("/api/status/<job_id>")
def status(job_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT status, total, processed, created_at, finished_at, output_file FROM jobs WHERE id=?",
        (job_id,)
    ).fetchone()

    logs = conn.execute(
        "SELECT nivel, mensagem, criado_em FROM logs WHERE job_id=? ORDER BY id DESC LIMIT 50",
        (job_id,)
    ).fetchall()

    # Últimos resultados
    ultimos = conn.execute(
        """SELECT chave_nfe, status_nfe, numero_due, consultado_em 
           FROM resultados WHERE job_id=? ORDER BY id DESC LIMIT 20""",
        (job_id,)
    ).fetchall()

    conn.close()

    if not row:
        return jsonify({"error": "Job não encontrado"}), 404

    status_val, total, processed, created_at, finished_at, output_file = row
    pct = round(processed / total * 100, 1) if total else 0

    return jsonify({
        "job_id": job_id,
        "status": status_val,
        "total": total,
        "processed": processed,
        "percent": pct,
        "created_at": created_at,
        "finished_at": finished_at,
        "has_output": bool(output_file and (
            Path(output_file).exists() if Path(output_file).is_absolute()
            else (_BASE_DIR / output_file).exists() or Path(output_file).resolve().exists()
        )),
        "logs": [{"nivel": l[0], "mensagem": l[1], "criado_em": l[2]} for l in logs],
        "ultimos_resultados": [
            {"chave": r[0][:10] + "..." + r[0][-6:], "status": r[1], "due": r[2], "em": r[3]}
            for r in ultimos
        ]
    })

@app.route("/api/cancelar/<job_id>", methods=["POST"])
def cancelar(job_id):
    update_job(job_id, status="cancelled")
    return jsonify({"message": "Solicitação de cancelamento enviada"})

@app.route("/api/download/<job_id>")
def download(job_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT output_file FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()

    if not row or not row[0]:
        return jsonify({"error": "Relatório não disponível"}), 404

    output_path = Path(row[0])
    if not output_path.is_absolute():
        # Try backend dir first, then resolve against CWD (legacy paths from root runs)
        candidate = _BASE_DIR / output_path
        output_path = candidate if candidate.exists() else output_path.resolve()

    if not output_path.exists():
        output_path = gerar_relatorio(job_id)

    return send_file(str(output_path), as_attachment=True, download_name=f"resultado_due_{job_id[:8]}.xlsx")

@app.route("/api/jobs")
def listar_jobs():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, status, total, processed, created_at, finished_at FROM jobs ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    conn.close()

    return jsonify([
        {"id": r[0], "status": r[1], "total": r[2], "processed": r[3],
         "created_at": r[4], "finished_at": r[5]}
        for r in rows
    ])

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "version": "1.0.0"})

from piscofins import bp as piscofins_bp
app.register_blueprint(piscofins_bp)

# Serve static docs so browsers don't hit file:// CORS restrictions
_DOCS_DIR = str(_BASE_DIR.parent / "docs")

@app.route("/")
def serve_index():
    return send_file(str(Path(_DOCS_DIR) / "portal.html"))

@app.route("/<path:filename>")
def serve_docs(filename):
    from flask import abort
    filepath = Path(_DOCS_DIR) / filename
    if filepath.suffix in (".html", ".css", ".js", ".png", ".svg", ".ico") and filepath.exists():
        return send_file(str(filepath))
    abort(404)

if __name__ == "__main__":
    init_db()
    print("=" * 60)
    print("  DUE Consulta Backend — Rodando em http://localhost:5000")
    print("  Portal                — http://localhost:5000/portal.html")
    print("  PIS/COFINS            — http://localhost:5000/piscofins.html")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False)
