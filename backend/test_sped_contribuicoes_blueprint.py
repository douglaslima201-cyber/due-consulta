"""
Smoke test do Blueprint sped_contribuicoes (sem depender de main.py / playwright).

Executar: python test_sped_contribuicoes_blueprint.py
"""
import io
import os
import json

from flask import Flask

from sped_contribuicoes import bp

BASE = os.path.dirname(__file__)


def main():
    app = Flask(__name__)
    app.register_blueprint(bp)
    client = app.test_client()

    # --- Teste 1: upload de um único arquivo (01/2025) ---
    with open(os.path.join(BASE, "sped_contribuicoes_exemplo.txt"), "rb") as f:
        data = {"files": (io.BytesIO(f.read()), "sped_jan2025.txt")}
        resp = client.post("/api/sped-contribuicoes/upload", data=data, content_type="multipart/form-data")

    print("=== Upload único (01/2025) ===")
    print("status:", resp.status_code)
    body = resp.get_json()
    print("resumo:", json.dumps(body["resumo"], ensure_ascii=False, indent=2))
    print("erros:", body["erros"])
    analysis_id = body["analysis_id"]

    # Download Excel
    resp_xlsx = client.get(f"/api/sped-contribuicoes/download/excel/{analysis_id}")
    print("excel status:", resp_xlsx.status_code, "content-type:", resp_xlsx.content_type,
          "tamanho:", len(resp_xlsx.data))

    # --- Teste 2: upload de dois arquivos (12/2024 + 01/2025) -> testa G4 ---
    with open(os.path.join(BASE, "sped_contribuicoes_exemplo_dez2024.txt"), "rb") as f1, \
         open(os.path.join(BASE, "sped_contribuicoes_exemplo.txt"), "rb") as f2:
        data = {
            "files": [
                (io.BytesIO(f1.read()), "sped_dez2024.txt"),
                (io.BytesIO(f2.read()), "sped_jan2025.txt"),
            ]
        }
        resp2 = client.post("/api/sped-contribuicoes/upload", data=data, content_type="multipart/form-data")

    print("\n=== Upload múltiplo (12/2024 + 01/2025) ===")
    print("status:", resp2.status_code)
    body2 = resp2.get_json()
    print("resumo:", json.dumps(body2["resumo"], ensure_ascii=False, indent=2))
    print("por_periodo:", json.dumps(body2["por_periodo"], ensure_ascii=False, indent=2, default=str))

    print("\nAchados G4 (transposição 1100/1500):")
    for a in body2["achados"]:
        if a["grupo"] == "G4":
            print(f"  [{a['tipo']}] {a['competencia']} {a['registro']} "
                  f"R$ {a['valor_envolvido']:.2f} - {a['descricao']}")


if __name__ == "__main__":
    main()
