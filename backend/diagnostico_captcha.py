"""Script de diagnóstico do CAPTCHA e campos no portal Siscomex"""
import asyncio
import sys
sys.path.insert(0, '.')
from main import _obter_sitekey, init_db, PORTAL_URL
from playwright.async_api import async_playwright

CHAVE_TESTE = "35240212345678000195550010000001001000000010"

async def diagnostico():
    init_db()
    print("=== DIAGNOSTICO CAPTCHA + CAMPOS ===")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        print(f"Navegando para: {PORTAL_URL}")
        await page.goto(PORTAL_URL, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)

        await page.screenshot(path="diag_1_inicial.png")
        print("Screenshot salvo: diag_1_inicial.png")

        # Frames
        print(f"\nFrames carregados ({len(page.frames)}):")
        for f in page.frames:
            if f.url and f.url != "about:blank":
                url = f.url.lower()
                tipo = "hCaptcha" if "hcaptcha" in url else ("reCAPTCHA" if "recaptcha" in url else "outro")
                print(f"  [{tipo}] {f.url[:100]}")

        # Mostrar URLs completas dos frames hCaptcha
        print("\nURLs completas dos frames hCaptcha:")
        for f in page.frames:
            if "hcaptcha.com" in (f.url or "").lower():
                print(f"  {f.url}")

        # Sitekey inicial
        sk = await _obter_sitekey(page)
        print(f"\nSitekey na pagina inicial: {sk}")

        # Elementos captcha no DOM
        for sel in ['.h-captcha', '.g-recaptcha', '[data-sitekey]', 'iframe[src*="hcaptcha"]']:
            try:
                n = await page.locator(sel).count()
                if n > 0:
                    print(f"  Elemento '{sel}': {n} encontrado(s)")
            except Exception:
                pass

        # Tentar clicar NF-e radio
        print("\n--- Tentando clicar radio NF-e ---")
        nfe_ok = False
        for sel in ['label:has-text("NF-e")', 'label:has-text("NF-E")',
                    'input[value="NFE"]', 'input[value="NF-e"]', 'input[value="NF_E"]']:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.click()
                    nfe_ok = True
                    print(f"  Clicado via: {sel}")
                    await asyncio.sleep(1)
                    break
            except Exception:
                continue
        if not nfe_ok:
            print("  AVISO: Radio NF-e nao encontrado")

        await page.screenshot(path="diag_2_apos_nfe.png")
        print("Screenshot salvo: diag_2_apos_nfe.png")

        # Todos os inputs visíveis
        print("\n--- Inputs visíveis na página ---")
        inputs_info = await page.evaluate("""
            () => {
                const inputs = document.querySelectorAll('input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"])');
                return Array.from(inputs).map(el => ({
                    tag: el.tagName,
                    type: el.type,
                    id: el.id,
                    name: el.name,
                    placeholder: el.placeholder,
                    maxlength: el.maxLength,
                    className: el.className.substring(0, 60),
                    visible: el.offsetParent !== null
                }));
            }
        """)
        for inp in inputs_info:
            if inp.get("visible"):
                print(f"  input type={inp['type']} id='{inp['id']}' "
                      f"name='{inp['name']}' placeholder='{inp['placeholder']}' "
                      f"maxlen={inp['maxlength']}")

        # Tentar encontrar o campo correto
        print("\n--- Testando seletores de campo ---")
        input_sels = [
            'input[placeholder*="chave" i]',
            'input[placeholder*="NF-e" i]',
            'input[placeholder*="acesso" i]',
            'input[maxlength="44"]',
            'input[id*="chave" i]',
            'input[name*="chave" i]',
            'main input[type="text"]:not([placeholder*="Buscar" i])',
            'section input[type="text"]',
            'form input[type="text"]',
        ]
        campo = None
        for sel in input_sels:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1500):
                    info = await el.evaluate(
                        "el => ({id: el.id, placeholder: el.placeholder, maxlen: el.maxLength})"
                    )
                    print(f"  ENCONTRADO '{sel}' -> id='{info['id']}' "
                          f"placeholder='{info['placeholder']}' maxlen={info['maxlen']}")
                    if campo is None:
                        campo = el
            except Exception:
                pass

        if campo:
            print(f"\nUsando campo para digitar chave...")
            await campo.click()
            await campo.fill("")
            await campo.type(CHAVE_TESTE[:10], delay=20)
            await asyncio.sleep(3)

            sk2 = await _obter_sitekey(page)
            print(f"Sitekey apos interagir com campo: {sk2}")

            await page.screenshot(path="diag_3_campo_preenchido.png")
            print("Screenshot salvo: diag_3_campo_preenchido.png")
        else:
            print("AVISO: Nenhum campo encontrado com seletores conhecidos")
            await page.screenshot(path="diag_sem_campo.png")

        print("\n=== FIM — aguardando 10s para fechar ===")
        await asyncio.sleep(10)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(diagnostico())
