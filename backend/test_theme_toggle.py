import os, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from playwright.sync_api import sync_playwright

BASE = os.path.dirname(__file__)
DOCS = os.path.join(os.path.dirname(BASE), 'docs')
PAGE_URL = 'file:///' + os.path.join(DOCS, 'sped-contribuicoes.html').replace('\\', '/')

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    errors = []
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(PAGE_URL)

    btn = page.locator('#btn-theme')
    print('Botao visivel:', btn.is_visible())
    print('Icone inicial (dark):', btn.inner_text())

    # clica -> modo claro
    btn.click()
    page.wait_for_timeout(200)
    print('Icone apos click (light):', btn.inner_text())
    has_light = page.evaluate("document.body.classList.contains('light')")
    print('body.light ativo:', has_light)
    saved = page.evaluate("localStorage.getItem('sped-theme')")
    print('localStorage sped-theme:', saved)

    nav_bg = page.evaluate("getComputedStyle(document.querySelector('nav')).backgroundColor")
    print('Nav bg (light):', nav_bg)

    body_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    print('Body bg (light):', body_bg)

    # clica -> volta modo escuro
    btn.click()
    page.wait_for_timeout(200)
    print('Icone apos 2o click (dark):', btn.inner_text())
    has_light2 = page.evaluate("document.body.classList.contains('light')")
    print('body.light apos voltar:', has_light2)
    saved2 = page.evaluate("localStorage.getItem('sped-theme')")
    print('localStorage apos voltar:', saved2)

    nav_bg2 = page.evaluate("getComputedStyle(document.querySelector('nav')).backgroundColor")
    print('Nav bg (dark):', nav_bg2)

    print('Erros:', errors if errors else '(nenhum)')
    browser.close()
