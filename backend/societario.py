"""
Motor de Mapeamento Societário — Análise de Grupos Econômicos
Fonte de dados: BrasilAPI (brasilapi.com.br)
"""
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
import urllib.request
import urllib.error

from flask import Blueprint, jsonify, request

bp = Blueprint("societario", __name__, url_prefix="/api/societario")

_BASE_DIR = Path(__file__).parent
_DB = str(_BASE_DIR / "societario.db")
_BRASILAPI = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"

_HOLDING_CNAES = {"6461", "6462"}


def _init_db():
    conn = sqlite3.connect(_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS cnpj_cache (
        cnpj TEXT PRIMARY KEY,
        dados TEXT,
        ts TEXT,
        http_status INTEGER DEFAULT 200
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS historico (
        id TEXT PRIMARY KEY,
        cnpj TEXT,
        razao_social TEXT,
        total_empresas INTEGER,
        total_pf INTEGER,
        ts TEXT
    )""")
    conn.commit()
    conn.close()


_init_db()


def _fmt(cnpj: str) -> str:
    c = re.sub(r"\D", "", str(cnpj or ""))
    if len(c) == 14:
        return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"
    return cnpj


def _limpar(cnpj: str) -> str:
    return re.sub(r"\D", "", str(cnpj or ""))


def _fetch(cnpj: str) -> dict | None:
    """Consulta CNPJ via BrasilAPI com cache de 24h em SQLite."""
    cnpj = _limpar(cnpj)
    if len(cnpj) != 14:
        return None

    conn = sqlite3.connect(_DB)
    row = conn.execute(
        "SELECT dados, ts, http_status FROM cnpj_cache WHERE cnpj=?", (cnpj,)
    ).fetchone()
    conn.close()

    if row:
        dados_json, ts, status = row
        age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
        if age < 86400:
            return json.loads(dados_json) if status == 200 else None

    url = _BRASILAPI.format(cnpj=cnpj)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "RumoBrasil-SocietarioMapper/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            dados = json.loads(resp.read().decode())
            conn = sqlite3.connect(_DB)
            conn.execute(
                "INSERT OR REPLACE INTO cnpj_cache VALUES (?,?,?,?)",
                (cnpj, json.dumps(dados, ensure_ascii=False), datetime.now().isoformat(), 200),
            )
            conn.commit()
            conn.close()
            return dados
    except urllib.error.HTTPError as e:
        conn = sqlite3.connect(_DB)
        conn.execute(
            "INSERT OR REPLACE INTO cnpj_cache VALUES (?,?,?,?)",
            (cnpj, "{}", datetime.now().isoformat(), e.code),
        )
        conn.commit()
        conn.close()
        return None
    except Exception as ex:
        print(f"[Societario] Erro CNPJ {cnpj}: {ex}")
        return None


def _is_holding(dados: dict) -> bool:
    cnae = str(dados.get("cnae_fiscal", ""))[:4]
    desc = (dados.get("cnae_fiscal_descricao", "") or "").lower()
    nat = (dados.get("descricao_natureza_juridica", "") or "").lower()
    kws = ["holding", "participações", "participacoes", "participação", "participacao"]
    return cnae in _HOLDING_CNAES or any(k in desc for k in kws) or any(k in nat for k in kws)


def _node_pj(cnpj: str, dados: dict, depth: int) -> dict:
    cnpj = _limpar(cnpj)
    cnaes_sec = [
        {"codigo": str(s.get("codigo", "")), "desc": s.get("descricao", "")}
        for s in (dados.get("cnaes_secundarios") or [])
    ]
    return {
        "id": cnpj,
        "type": "pj",
        "cnpj": cnpj,
        "cnpj_fmt": _fmt(cnpj),
        "razao_social": dados.get("razao_social", ""),
        "nome_fantasia": dados.get("nome_fantasia", ""),
        "situacao": (
            dados.get("descricao_situacao_cadastral", "")
            or dados.get("situacao_cadastral", "")
        ),
        "cnae_cod": str(dados.get("cnae_fiscal", "")),
        "cnae_desc": dados.get("cnae_fiscal_descricao", ""),
        "natureza": (
            dados.get("descricao_natureza_juridica", "")
            or dados.get("natureza_juridica", "")
        ),
        "capital": float(dados.get("capital_social", 0) or 0),
        "abertura": dados.get("data_inicio_atividade", ""),
        "uf": dados.get("uf", ""),
        "municipio": dados.get("municipio", ""),
        "logradouro": f"{dados.get('logradouro','') or ''} {dados.get('numero','') or ''}".strip(),
        "cep": dados.get("cep", ""),
        "qsa_count": len(dados.get("qsa", [])),
        "is_holding": _is_holding(dados),
        "depth": depth,
        "cnaes_sec": cnaes_sec,
    }


def _mapear_grupo(cnpj_raiz: str, max_depth: int = 3) -> dict:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    visited_cnpjs: set[str] = set()
    visited_edges: set[str] = set()

    def _visit(cnpj: str, depth: int):
        cnpj = _limpar(cnpj)
        if not cnpj or cnpj in visited_cnpjs or depth > max_depth:
            return
        visited_cnpjs.add(cnpj)

        dados = _fetch(cnpj)
        if not dados:
            if cnpj not in nodes:
                nodes[cnpj] = {
                    "id": cnpj, "type": "pj", "cnpj": cnpj,
                    "cnpj_fmt": _fmt(cnpj),
                    "razao_social": "Dados não disponíveis",
                    "situacao": "Desconhecida",
                    "cnae_desc": "", "uf": "", "municipio": "",
                    "capital": 0, "depth": depth,
                    "is_holding": False, "qsa_count": 0,
                    "natureza": "", "abertura": "", "cnaes_sec": [],
                }
            return

        nodes[cnpj] = _node_pj(cnpj, dados, depth)

        for socio in dados.get("qsa", []):
            nome = socio.get("nome_socio", "")
            cpf_cnpj_raw = socio.get("cpf_cnpj_socio", "")
            cpf_cnpj = _limpar(cpf_cnpj_raw)
            tipo_id = socio.get("identificador_de_socio", 1)
            qualif = socio.get("qualificacao_socio", "")
            pct = float(socio.get("percentual_capital_social", 0) or 0)

            if tipo_id == 2 and len(cpf_cnpj) == 14:
                sid = cpf_cnpj
                if sid not in nodes:
                    nodes[sid] = {
                        "id": sid, "type": "pj",
                        "cnpj": sid, "cnpj_fmt": _fmt(sid),
                        "razao_social": nome, "situacao": "",
                        "cnae_desc": "", "capital": 0, "uf": "",
                        "municipio": "", "natureza": "", "abertura": "",
                        "depth": depth + 1, "is_holding": False, "qsa_count": 0,
                        "cnaes_sec": [],
                    }
                ek = f"{sid}>{cnpj}"
                if ek not in visited_edges:
                    visited_edges.add(ek)
                    edges.append({
                        "from": sid, "to": cnpj,
                        "pct": pct, "qualif": qualif,
                        "label": f"{pct:.0f}%" if pct else qualif[:25],
                    })
                if depth + 1 <= max_depth:
                    time.sleep(0.25)
                    _visit(sid, depth + 1)
            else:
                sufixo = cpf_cnpj[-4:] if len(cpf_cnpj) >= 4 else cpf_cnpj or nome[-4:]
                pf_id = f"pf_{re.sub(r'[^a-z0-9]', '_', nome.lower()[:30])}_{sufixo}"
                if pf_id not in nodes:
                    nodes[pf_id] = {
                        "id": pf_id, "type": "pf",
                        "nome": nome, "cpf": cpf_cnpj_raw,
                        "depth": depth + 1,
                        "empresas_no_grafo": [],
                    }
                nodes[pf_id].setdefault("empresas_no_grafo", []).append(cnpj)
                ek = f"{pf_id}>{cnpj}"
                if ek not in visited_edges:
                    visited_edges.add(ek)
                    edges.append({
                        "from": pf_id, "to": cnpj,
                        "pct": pct, "qualif": qualif,
                        "label": f"{pct:.0f}%" if pct else qualif[:25],
                    })

    _visit(cnpj_raiz, 0)

    # Enriquecer PJs que apareceram em QSA mas não foram visitados (limite de depth)
    for nid, node in list(nodes.items()):
        if node.get("type") == "pj" and not node.get("cnae_desc") and nid not in visited_cnpjs:
            d = _fetch(nid)
            if d:
                nodes[nid] = _node_pj(nid, d, node.get("depth", 0))

    # Gerar alertas analíticos
    alertas = []

    for n in nodes.values():
        if n.get("type") == "pf":
            cnt = len(n.get("empresas_no_grafo", []))
            if cnt >= 2:
                alertas.append({
                    "tipo": "warning",
                    "titulo": "Sócio com múltiplas empresas",
                    "msg": f"{n['nome']} é sócio(a) em {cnt} empresa(s) dentro do grupo mapeado.",
                })

    for n in nodes.values():
        if n.get("type") == "pj" and n.get("situacao"):
            sit = n["situacao"].upper()
            if any(k in sit for k in ["BAIXADA", "INAPTA", "CANCELADA", "NULA", "SUSPENSA"]):
                alertas.append({
                    "tipo": "danger",
                    "titulo": "Empresa inativa no grupo",
                    "msg": f"{n.get('razao_social', n['cnpj'])} — situação: {n['situacao']}",
                })

    for n in nodes.values():
        if n.get("type") == "pj" and n.get("is_holding"):
            alertas.append({
                "tipo": "info",
                "titulo": "Holding identificada",
                "msg": f"{n.get('razao_social', '')} é uma sociedade de participações/holding (CNAE {n.get('cnae_cod','')}).",
            })

    # Alertas de CNAEs incompatíveis entre empresas do mesmo sócio PF
    pf_cnaes: dict[str, set] = {}
    for edge in edges:
        from_node = nodes.get(edge["from"], {})
        to_node = nodes.get(edge["to"], {})
        if from_node.get("type") == "pf":
            pf_id = edge["from"]
            cnae_2dig = to_node.get("cnae_cod", "")[:2]
            pf_cnaes.setdefault(pf_id, set()).add(cnae_2dig)
    for pf_id, cnaes_set in pf_cnaes.items():
        if len(cnaes_set) >= 3:
            pf_node = nodes.get(pf_id, {})
            alertas.append({
                "tipo": "warning",
                "titulo": "Diversificação setorial acentuada",
                "msg": f"{pf_node.get('nome',pf_id)} participa de empresas em {len(cnaes_set)} segmentos CNAE distintos.",
            })

    total_pj = sum(1 for n in nodes.values() if n.get("type") == "pj")
    total_pf = sum(1 for n in nodes.values() if n.get("type") == "pf")
    raiz_node = nodes.get(_limpar(cnpj_raiz), {})

    return {
        "cnpj_raiz": _limpar(cnpj_raiz),
        "razao_social_raiz": raiz_node.get("razao_social", ""),
        "nodes": list(nodes.values()),
        "edges": edges,
        "total_empresas": total_pj,
        "total_pf": total_pf,
        "alertas": alertas,
    }


@bp.route("/cnpj/<cnpj>")
def get_cnpj(cnpj):
    dados = _fetch(cnpj)
    if not dados:
        return jsonify({"error": "CNPJ não encontrado ou inválido"}), 404
    return jsonify(dados)


@bp.route("/mapear/<cnpj>")
def mapear(cnpj):
    cnpj_limpo = _limpar(cnpj)
    if len(cnpj_limpo) != 14:
        return jsonify({"error": "CNPJ inválido. Informe os 14 dígitos."}), 400
    try:
        profundidade = min(int(request.args.get("depth", 3)), 4)
        grafo = _mapear_grupo(cnpj_limpo, max_depth=profundidade)
        conn = sqlite3.connect(_DB)
        conn.execute(
            "INSERT INTO historico VALUES (?,?,?,?,?,?)",
            (
                str(uuid.uuid4())[:8], cnpj_limpo,
                grafo["razao_social_raiz"],
                grafo["total_empresas"],
                grafo["total_pf"],
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
        return jsonify(grafo)
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


@bp.route("/historico")
def historico():
    conn = sqlite3.connect(_DB)
    rows = conn.execute(
        "SELECT id, cnpj, razao_social, total_empresas, total_pf, ts "
        "FROM historico ORDER BY ts DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return jsonify([
        {"id": r[0], "cnpj": r[1], "razao_social": r[2],
         "total_empresas": r[3], "total_pf": r[4], "ts": r[5]}
        for r in rows
    ])
