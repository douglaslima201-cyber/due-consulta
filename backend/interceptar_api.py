"""
Intercepta as requisições de rede do portal Siscomex ao consultar uma NF-e.
Objetivo: descobrir o endpoint e headers reais para chamar a API diretamente.
"""
import asyncio
import json
from playwright.async_api import async_playwright

PORTAL_URL = "https://portalunico.siscomex.gov.br/due/x/#/consulta/consulta-filtro?perfil=publico"

captured = []
IGNORAR = [
    ".js", ".css", ".woff", ".woff2", ".png", ".jpg", ".svg", ".ico",
    "hcaptcha.com", "google.com", "analytics", "fonts.googleapis",
]

async def interceptar():
    print("=== INTERCEPTADOR DE API SISCOMEX ===")
    print("1. Chrome vai abrir no portal")
    print("2. Selecione NF-e, preencha uma chave, resolva o hCaptcha e clique Consultar")
    print("3. Aguarde o resultado aparecer na tela")
    print("4. As requisicoes serao salvas automaticamente\n")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=False)
            print("Usando Chrome instalado no sistema.")
        except Exception:
            browser = await p.chromium.launch(headless=False)
            print("Chrome nao encontrado, usando Chromium embutido.")

        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        async def on_request(request):
            url = request.url
            if any(ig in url for ig in IGNORAR):
                return
            entry = {
                "type": "REQUEST",
                "method": request.method,
                "url": url,
                "headers": dict(request.headers),
                "post_data": request.post_data,
            }
            captured.append(entry)
            marker = "***" if request.method == "POST" else "   "
            print(f"{marker} REQ [{request.method}] {url}")

        async def on_response(response):
            url = response.url
            if any(ig in url for ig in IGNORAR):
                return
            try:
                body = await response.text()
            except Exception:
                body = ""
            entry = {
                "type": "RESPONSE",
                "status": response.status,
                "url": url,
                "headers": dict(response.headers),
                "body": body[:3000],
            }
            captured.append(entry)
            if response.status not in (200, 204, 304):
                print(f"    RES [{response.status}] {url}")
            elif any(kw in url for kw in ["consulta", "nfe", "due", "captcha", "proxy"]):
                print(f"    RES [{response.status}] {url}")
                if body and len(body) > 5:
                    print(f"        body: {body[:400]}")

        page.on("request", on_request)
        page.on("response", on_response)

        await page.goto(PORTAL_URL, wait_until="networkidle", timeout=30000)
        print("\nBrowser aberto. Faca a consulta agora. Aguardando 90 segundos...\n")
        await asyncio.sleep(90)

        with open("api_capturada.json", "w", encoding="utf-8") as f:
            json.dump(captured, f, indent=2, ensure_ascii=False)
        print(f"\n{len(captured)} entradas salvas em api_capturada.json")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(interceptar())
