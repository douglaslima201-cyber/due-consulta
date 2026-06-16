"""
Teste end-to-end do frontend docs/sped-contribuicoes.html via Playwright.

Pré-requisito: backend rodando em http://localhost:5000.

Executar: python test_sped_contribuicoes_frontend.py
"""
import os
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

BASE = os.path.dirname(__file__)
DOCS = os.path.join(os.path.dirname(BASE), "docs")
PAGE_URL = "file:///" + os.path.join(DOCS, "sped-contribuicoes.html").replace("\\", "/")

ARQ_JAN = os.path.join(BASE, "sped_contribuicoes_exemplo.txt")
ARQ_DEZ = os.path.join(BASE, "sped_contribuicoes_exemplo_dez2024.txt")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))

        page.goto(PAGE_URL)
        print("Título da página:", page.title())

        # Upload de 2 arquivos (dez/2024 + jan/2025) para testar G4
        page.set_input_files("#file-input", [ARQ_DEZ, ARQ_JAN])

        # Verifica lista de arquivos
        fitems = page.locator(".fitem").all_text_contents()
        print("\nArquivos na lista:", fitems)

        # Clica em Analisar
        page.click("#btn-analisar")

        # Aguarda resultados aparecerem
        page.wait_for_selector("#results", state="visible", timeout=20000)
        page.wait_for_function("document.getElementById('k-total').textContent !== '—'", timeout=20000)

        print("\n=== KPIs ===")
        for kid in ["k-total", "k-periodos", "k-oportunidades", "k-oportunidades-n",
                    "k-riscos", "k-riscos-n", "k-inconsistencias", "k-inconsistencias-n", "k-alto"]:
            print(f"  {kid}: {page.locator('#' + kid).inner_text()}")

        print("\n=== Barra da empresa ===")
        print(" ", page.locator("#file-bar-name").inner_text())
        print(" ", page.locator("#file-bar-info").inner_text())

        print("\n=== Abas disponíveis ===")
        tabs = page.locator(".tab-btn").all_text_contents()
        print(" ", tabs)

        print("\n=== Insights ===")
        for card in page.locator(".insight-card").all_text_contents():
            print(" -", " | ".join(line.strip() for line in card.splitlines() if line.strip()))

        print("\n=== Tabela de períodos ===")
        rows = page.locator("#periodos-body tr").all_text_contents()
        for r in rows:
            print(" -", " | ".join(c.strip() for c in r.split("\n") if c.strip()))

        print("\n=== Contagem da tabela de achados ===")
        print(" ", page.locator("#table-count").inner_text())

        # ── Aba Apuração ───────────────────────────────────────────────────────
        print("\n=== Aba Apuração ===")
        page.click('.tab-btn[data-tab="apuracao"]')
        page.wait_for_timeout(300)
        apur_text = page.locator("#apuracao-body").inner_text()
        print(" ", apur_text[:350].replace("\n", " | "))

        # ── Abas G1-G5 ────────────────────────────────────────────────────────
        print("\n=== Abas G1-G5 ===")
        for g in ["G1", "G2", "G3", "G4", "G5"]:
            page.click(f'.tab-btn[data-tab="{g}"]')
            page.wait_for_timeout(150)
            badge = page.locator(f"#tab-count-{g}").inner_text()
            cards = page.locator(f"#tab-{g} .achado-card").count()
            print(f"  {g}: badge={badge}, cards={cards}")

        # G5 rateio proporcional
        page.click('.tab-btn[data-tab="G5"]')
        page.wait_for_timeout(200)
        rateio_text = page.locator("#rateio-dados").inner_text()
        has_pct = "%" in rateio_text
        print(f"\n  G5 Rateio — percentuais visíveis: {has_pct}")
        print(" ", rateio_text[:200].replace("\n", " | "))

        # ── Aba CFOP/CST ───────────────────────────────────────────────────────
        print("\n=== Aba CFOP/CST ===")
        page.click('.tab-btn[data-tab="cfop"]')
        page.wait_for_timeout(400)
        cfop_rows = page.locator("#cfop-body tr").count()
        cfop_count = page.locator("#cfop-count").inner_text()
        print(f"  rows={cfop_rows}, count=\"{cfop_count}\"")

        # Filtro por Entradas
        page.select_option("#cfop-filter-oper", "Entrada")
        page.wait_for_timeout(200)
        cfop_entrada = page.locator("#cfop-body tr").count()
        print(f"  filtro=Entrada rows={cfop_entrada}")
        page.select_option("#cfop-filter-oper", "")

        # ── Aba Conclusão ─────────────────────────────────────────────────────
        print("\n=== Aba Conclusão ===")
        page.click('.tab-btn[data-tab="conclusao"]')
        page.wait_for_timeout(200)
        conclusao_text = page.locator("#conclusao-body").inner_text()
        print(" ", conclusao_text[:300].replace("\n", " | "))

        # Testa filtro por grupo G4 na aba Resumo
        page.click('.tab-btn[data-tab="resumo"]')
        page.wait_for_timeout(200)
        page.select_option("#filter-grupo", "G4")
        page.wait_for_timeout(300)
        print("\n=== Filtro G4 (Resumo) ===")
        print(" ", page.locator("#table-count").inner_text())
        g4_rows = page.locator(".data-row:visible").all_text_contents()
        for r in g4_rows:
            print(" -", " | ".join(c.strip() for c in r.split("\n") if c.strip()))
        page.select_option("#filter-grupo", "")

        # Testa download de Excel
        print("\n=== Download Excel ===")
        with page.expect_download() as dl_info:
            page.click("#btn-excel")
        download = dl_info.value
        path = download.path()
        print("  arquivo:", download.suggested_filename, "tamanho:", os.path.getsize(path), "bytes")

        print("\n=== Erros de console ===")
        print(console_errors if console_errors else "  (nenhum)")

        browser.close()


if __name__ == "__main__":
    main()
