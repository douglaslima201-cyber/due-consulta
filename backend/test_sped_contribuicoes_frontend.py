"""
Teste end-to-end do frontend docs/sped-contribuicoes.html via Playwright.

Pré-requisito: backend rodando em http://localhost:5000.

Executar: python test_sped_contribuicoes_frontend.py
"""
import os
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

        print("\n=== Insights ===")
        for card in page.locator(".insight-card").all_text_contents():
            print(" -", " | ".join(line.strip() for line in card.splitlines() if line.strip()))

        print("\n=== Tabela de períodos ===")
        rows = page.locator("#periodos-body tr").all_text_contents()
        for r in rows:
            print(" -", " | ".join(c.strip() for c in r.split("\n") if c.strip()))

        print("\n=== Contagem da tabela de achados ===")
        print(" ", page.locator("#table-count").inner_text())

        # Testa filtro por grupo G4
        page.select_option("#filter-grupo", "G4")
        page.wait_for_timeout(300)
        print("\n=== Filtro G4 ===")
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
