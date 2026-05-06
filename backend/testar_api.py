"""
Testa o endpoint api/ext/due/consultar diretamente, sem browser.
Descobre parâmetros e necessidade de captcha por sessão.
"""
import asyncio
import json
import aiohttp

BASE = "https://portalunico.siscomex.gov.br/due"
HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://portalunico.siscomex.gov.br/due/x/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/plain, */*",
}

# Chave de teste (44 dígitos — pode substituir por uma real)
CHAVE_TESTE = "35240212345678000195550010000001001000000010"

async def main():
    async with aiohttp.ClientSession() as s:
        # 1. Obter CSRF token inicial
        print("1. Obtendo CSRF token inicial...")
        async with s.get(
            f"{BASE}/proxy/user?checkLogout=true&tabId=&perfil=publico",
            headers=HEADERS_BASE
        ) as r:
            csrf = r.headers.get("x-csrf-token", "")
            body = await r.json(content_type=None)
            print(f"   Status: {r.status} | csrf: {csrf[:30]}...")
            print(f"   Usuário: {body.get('id')} | exibeCaptcha: {body.get('exibeCaptcha')}")

        headers = {**HEADERS_BASE, "X-CSRF-Token": csrf}

        # 2. Obter info de sessão
        print("\n2. Obtendo sessão...")
        async with s.get(f"{BASE}/proxy/session", headers=headers) as r:
            csrf = r.headers.get("x-csrf-token", csrf)
            body = await r.json(content_type=None)
            print(f"   Status: {r.status} | exibeCaptcha: {body.get('exibeCaptcha')}")

        headers["X-CSRF-Token"] = csrf

        # 3. Tentar consultar SEM captcha (para ver o erro exato)
        print(f"\n3. Testando consulta SEM captcha (chave: {CHAVE_TESTE[:10]}...)...")

        # Variantes de parâmetros a testar
        variantes = [
            ("GET", f"{BASE}/api/ext/due/consultar", {"nrNfe": CHAVE_TESTE}),
            ("GET", f"{BASE}/api/ext/due/consultar", {"chave": CHAVE_TESTE}),
            ("GET", f"{BASE}/api/ext/due/consultar", {"chaveNfe": CHAVE_TESTE}),
            ("GET", f"{BASE}/api/ext/due/consultar", {"parametro": CHAVE_TESTE, "tipo": "NFE"}),
            ("POST", f"{BASE}/api/ext/due/consultar", {"nrNfe": CHAVE_TESTE, "tipo": "NFE"}),
        ]

        for method, url, params in variantes:
            try:
                if method == "GET":
                    async with s.get(url, headers=headers, params=params) as r:
                        body = await r.text()
                        print(f"\n   [{method}] {url}")
                        print(f"   Params: {params}")
                        print(f"   Status: {r.status}")
                        print(f"   Body: {body[:400]}")
                else:
                    async with s.post(url, headers=headers, json=params) as r:
                        body = await r.text()
                        print(f"\n   [{method}] {url}")
                        print(f"   Body enviado: {params}")
                        print(f"   Status: {r.status}")
                        print(f"   Body retornado: {body[:400]}")
            except Exception as e:
                print(f"   Erro: {e}")

        # 4. Tentar também listar-due-consulta que pode ser mais acessível
        print("\n\n4. Testando api/due/listar-due-consulta...")
        variantes2 = [
            {"nrNfe": CHAVE_TESTE},
            {"chaveNfe": CHAVE_TESTE, "tipoDoc": "NFE"},
        ]
        for params in variantes2:
            try:
                async with s.get(f"{BASE}/api/due/listar-due-consulta", headers=headers, params=params) as r:
                    body = await r.text()
                    print(f"   Params: {params} | Status: {r.status} | Body: {body[:300]}")
            except Exception as e:
                print(f"   Erro: {e}")

asyncio.run(main())
