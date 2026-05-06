"""
Testa variações do endpoint com sessão válida (após captcha resolvido no browser).
Execute DEPOIS de resolver o captcha uma vez no interceptar_api.py para ter cookies válidos.
"""
import asyncio
import json
import aiohttp
from playwright.async_api import async_playwright

PORTAL_URL = "https://portalunico.siscomex.gov.br/due/x/#/consulta/consulta-filtro?perfil=publico"
API_BASE   = "https://portalunico.siscomex.gov.br/due"
HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://portalunico.siscomex.gov.br/due/x/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/plain, */*",
}

# Substitua por uma chave NF-e real para teste
CHAVE = "51260106315338022198550240000001481452131356"

async def main():
    csrf_holder = {"token": None}
    captcha_ok  = asyncio.Event()

    print("Abrindo Chrome — resolva o hCaptcha e aguarde...")
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=False)
        except Exception:
            browser = await p.chromium.launch(headless=False)

        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=HEADERS_BASE["User-Agent"],
        )
        page = await context.new_page()
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        async def on_resp(response):
            t = response.headers.get("x-csrf-token")
            if t:
                csrf_holder["token"] = t
            if "portal/proxy/captcha" in response.url and response.status in (200, 204):
                print("\nCAPTCHA validado! Capturando sessão...")
                captcha_ok.set()

        page.on("response", on_resp)
        await page.goto(PORTAL_URL, wait_until="networkidle", timeout=30000)

        try:
            await asyncio.wait_for(captcha_ok.wait(), timeout=300)
        except asyncio.TimeoutError:
            print("Timeout esperando captcha")
            await browser.close()
            return

        await asyncio.sleep(2)
        cookies_list = await context.cookies()
        cookies = {c["name"]: c["value"] for c in cookies_list}
        csrf = csrf_holder["token"]
        await browser.close()

    print(f"\nSessão obtida. CSRF: {csrf[:30] if csrf else 'None'}...")
    print(f"Cookies: {list(cookies.keys())}")

    headers = {**HEADERS_BASE, "X-CSRF-Token": csrf or ""}

    # Testar diversas combinações
    testes = [
        # (método, url, params_get, body_post)
        ("GET",  f"{API_BASE}/api/due/listar-due-consulta",          {"nrNfe": CHAVE}, None),
        ("GET",  f"{API_BASE}/api/due/listar-due-consulta",          {"chaveNfe": CHAVE}, None),
        ("GET",  f"{API_BASE}/api/due/listar-due-consulta",          {"chave": CHAVE, "tipoDoc": "NFE"}, None),
        ("GET",  f"{API_BASE}/api/due/listar-due-consulta",          {"nrNfe": CHAVE, "tipoDoc": "NFE"}, None),
        ("POST", f"{API_BASE}/api/due/listar-due-consulta",          None, {"nrNfe": CHAVE, "tipoDoc": "NFE"}),
        ("GET",  f"{API_BASE}/api/ext/due/consultar",                {"nrNfe": CHAVE}, None),
        ("POST", f"{API_BASE}/api/ext/due/consultar",                None, {"nrNfe": CHAVE}),
        ("POST", f"{API_BASE}/api/ext/due/consultar",                None, {"chave": CHAVE, "tipo": "NFE"}),
        ("GET",  f"{API_BASE}/api/due/listar-due-consulta-estrangeiro", {"nrNfe": CHAVE}, None),
        ("GET",  f"{API_BASE}/api/due/obter",                        {"nrNfe": CHAVE}, None),
    ]

    async with aiohttp.ClientSession(cookies=cookies) as s:
        for method, url, params, body in testes:
            try:
                if method == "GET":
                    async with s.get(url, headers=headers, params=params) as r:
                        t = r.headers.get("x-csrf-token", csrf)
                        if t: csrf = t; headers["X-CSRF-Token"] = csrf
                        rb = await r.text()
                        print(f"\n[{r.status}] GET {url.split('/due/')[-1]}")
                        print(f"  params: {params}")
                        print(f"  body:   {rb[:600]}")
                else:
                    h = {**headers, "Content-Type": "application/json"}
                    async with s.post(url, headers=h, json=body) as r:
                        t = r.headers.get("x-csrf-token", csrf)
                        if t: csrf = t; headers["X-CSRF-Token"] = csrf
                        rb = await r.text()
                        print(f"\n[{r.status}] POST {url.split('/due/')[-1]}")
                        print(f"  body:   {body}")
                        print(f"  resp:   {rb[:600]}")
            except Exception as e:
                print(f"  ERRO: {e}")

asyncio.run(main())
