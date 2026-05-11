"""
Motor de Mapeamento Societário — Análise de Grupos Econômicos
Fonte primária : BrasilAPI (brasilapi.com.br)
Fonte ampliada: Dados Abertos Receita Federal — QSA completo
"""
import csv
import io
import json
import re
import sqlite3
import threading
import time
import uuid
import zipfile
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

def _rf_socios_urls() -> list[str]:
    """Gera lista de URLs tentando múltiplos espelhos da RF."""
    bases = [
        "https://dadosabertos.rfb.gov.br/CNPJ",
        "https://dados.rfb.gov.br/CNPJ/dados_abertos_cnpj/2025-05",
        "https://dados.rfb.gov.br/CNPJ/dados_abertos_cnpj/2025-04",
        "https://dados.rfb.gov.br/CNPJ/dados_abertos_cnpj/2025-03",
    ]
    urls = []
    for base in bases:
        for i in range(10):
            urls.append(f"{base}/Socios{i}.zip")
    return urls

# Estado global do import RF (thread-safe via GIL para leituras simples)
_rf_import_status: dict = {"estado": "idle", "arquivo": 0, "total": 10, "registros": 0, "erro": ""}


# ─── BANCO ────────────────────────────────────────────────────────────────────

def _init_db():
    conn = sqlite3.connect(_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS cnpj_cache (
        cnpj TEXT PRIMARY KEY, dados TEXT, ts TEXT, http_status INTEGER DEFAULT 200
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS historico (
        id TEXT PRIMARY KEY, cnpj TEXT, razao_social TEXT,
        total_empresas INTEGER, total_pf INTEGER, ts TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS socios_rf (
        cnpj_basico TEXT NOT NULL,
        tipo         INTEGER NOT NULL,
        nome         TEXT NOT NULL,
        cpf_cnpj     TEXT,
        qualificacao TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_socios_nome    ON socios_rf(nome)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_socios_cpfcnpj ON socios_rf(cpf_cnpj)")
    conn.execute("""CREATE TABLE IF NOT EXISTS rf_meta (
        chave TEXT PRIMARY KEY, valor TEXT
    )""")
    conn.commit()
    conn.close()


_init_db()


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _fmt(cnpj: str) -> str:
    c = re.sub(r"\D", "", str(cnpj or ""))
    if len(c) == 14:
        return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"
    return cnpj

def _limpar(cnpj: str) -> str:
    return re.sub(r"\D", "", str(cnpj or ""))

def _cnpj_dv(base12: str) -> str:
    """Calcula os dois dígitos verificadores de um CNPJ de 12 dígitos."""
    _W1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    _W2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    def dv(digits, weights):
        r = sum(int(d) * w for d, w in zip(digits, weights)) % 11
        return "0" if r < 2 else str(11 - r)
    d1 = dv(base12, _W1)
    return d1 + dv(base12 + d1, _W2)

def _basico_to_cnpj_matriz(basico: str) -> str:
    """cnpj_basico (8 dígitos) → CNPJ completo da matriz (sufixo 0001 + DV)."""
    b = basico.zfill(8)
    base12 = b + "0001"
    return base12 + _cnpj_dv(base12)


# ─── BRASILAPI COM CACHE ───────────────────────────────────────────────────────

def _fetch(cnpj: str) -> dict | None:
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
        req = urllib.request.Request(url, headers={"User-Agent": "RumoBrasil-SocietarioMapper/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            dados = json.loads(resp.read().decode())
            conn = sqlite3.connect(_DB)
            conn.execute(
                "INSERT OR REPLACE INTO cnpj_cache VALUES (?,?,?,?)",
                (cnpj, json.dumps(dados, ensure_ascii=False), datetime.now().isoformat(), 200),
            )
            conn.commit()
            conn.close()
            _indexar_socios_organico(cnpj, dados)
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


def _indexar_socios_organico(cnpj: str, dados: dict):
    """Salva o QSA de cada empresa consultada no índice local — cresce com o uso."""
    cnpj = _limpar(cnpj)
    cnpj_basico = cnpj[:8]
    qsa = dados.get("qsa", [])
    if not qsa:
        return
    try:
        conn = sqlite3.connect(_DB)
        for socio in qsa:
            tipo_id  = socio.get("identificador_de_socio", 1)
            nome     = (socio.get("nome_socio", "") or "").strip().upper()
            cpf_cnpj = _limpar(socio.get("cpf_cnpj_socio", "") or "")
            qualif   = (socio.get("qualificacao_socio", "") or "")[:10]
            tipo     = str(tipo_id)
            if not nome:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO socios_rf VALUES (?,?,?,?,?)",
                (cnpj_basico, tipo, nome, cpf_cnpj, qualif),
            )
        conn.execute(
            "INSERT OR REPLACE INTO rf_meta VALUES ('organico_em', ?)",
            (datetime.now().isoformat(),)
        )
        conn.commit()
        conn.close()
    except Exception as ex:
        print(f"[Organico] Erro ao indexar {cnpj}: {ex}")


# ─── DADOS RF — IMPORTAÇÃO ────────────────────────────────────────────────────

def _rf_disponivel() -> bool:
    try:
        conn = sqlite3.connect(_DB)
        row   = conn.execute("SELECT valor FROM rf_meta WHERE chave='importado_em'").fetchone()
        count = conn.execute("SELECT COUNT(*) FROM socios_rf LIMIT 1").fetchone()
        conn.close()
        return bool(row and count and count[0] > 0)
    except Exception:
        return False

def _rf_importado_em() -> str:
    try:
        conn = sqlite3.connect(_DB)
        row = conn.execute("SELECT valor FROM rf_meta WHERE chave='importado_em'").fetchone()
        conn.close()
        return row[0] if row else ""
    except Exception:
        return ""

def _rf_total_registros() -> int:
    try:
        conn = sqlite3.connect(_DB)
        n = conn.execute("SELECT COUNT(*) FROM socios_rf").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


def _processar_zip_socios(zipdata: bytes, conn, status_ref: dict) -> int:
    """Processa um arquivo ZIP de Socios da RF e insere no banco. Retorna registros inseridos."""
    total = 0
    with zipfile.ZipFile(io.BytesIO(zipdata)) as z:
        for fname in z.namelist():
            with z.open(fname) as f:
                reader = csv.reader(
                    io.TextIOWrapper(f, encoding="latin-1", errors="replace"),
                    delimiter=";",
                )
                batch = []
                for row in reader:
                    if len(row) < 4:
                        continue
                    cnpj_bas = (row[0] or "").strip().zfill(8)
                    tipo     = (row[1] or "1").strip()
                    nome     = (row[2] or "").strip().upper()
                    cpf_cnpj = (row[3] or "").strip()
                    qual     = (row[4] or "").strip() if len(row) > 4 else ""
                    if not cnpj_bas or not nome:
                        continue
                    batch.append((cnpj_bas, tipo, nome, cpf_cnpj, qual))
                    if len(batch) >= 10000:
                        conn.executemany("INSERT OR IGNORE INTO socios_rf VALUES (?,?,?,?,?)", batch)
                        conn.commit()
                        total += len(batch)
                        status_ref["registros"] = status_ref.get("registros", 0) + len(batch)
                        batch = []
                if batch:
                    conn.executemany("INSERT OR IGNORE INTO socios_rf VALUES (?,?,?,?,?)", batch)
                    conn.commit()
                    total += len(batch)
                    status_ref["registros"] = status_ref.get("registros", 0) + len(batch)
    return total


def _importar_rf_thread(arquivos_locais: list[str] | None = None):
    """
    Importa dados de Socios da RF.
    Se arquivos_locais fornecidos: importa ZIPs do disco.
    Caso contrário: tenta baixar dos servidores RF.
    """
    global _rf_import_status

    urls = _rf_socios_urls()
    total_urls = 10  # apenas contamos os primeiros 10 (um por índice)
    _rf_import_status = {
        "estado": "importando", "arquivo": 0, "total": total_urls,
        "registros": 0, "erro": "", "fonte": "local" if arquivos_locais else "web",
    }

    conn = sqlite3.connect(_DB, timeout=120, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.commit()

    total = 0
    arquivos_ok = 0

    try:
        if arquivos_locais:
            # ── Importar de arquivos locais ──
            _rf_import_status["total"] = len(arquivos_locais)
            for i, path in enumerate(arquivos_locais):
                _rf_import_status["arquivo"] = i + 1
                try:
                    with open(path, "rb") as f:
                        zipdata = f.read()
                    n = _processar_zip_socios(zipdata, conn, _rf_import_status)
                    total += n
                    arquivos_ok += 1
                    print(f"[RF Import] Arquivo local {i+1}: {n:,} registros")
                except Exception as ex:
                    print(f"[RF Import] Falha no arquivo local {path}: {ex}")
                    continue
        else:
            # ── Baixar da web ──
            baixados: set[int] = set()  # índices 0-9 já baixados com sucesso
            for url in urls:
                if len(baixados) >= 10:
                    break
                # Extrair índice do arquivo
                import re as _re
                m = _re.search(r'Socios(\d)\.zip', url)
                if not m:
                    continue
                idx = int(m.group(1))
                if idx in baixados:
                    continue
                _rf_import_status["arquivo"] = idx + 1
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "RumoBrasil/1.0"})
                    with urllib.request.urlopen(req, timeout=600) as resp:
                        zipdata = resp.read()
                    n = _processar_zip_socios(zipdata, conn, _rf_import_status)
                    total += n
                    baixados.add(idx)
                    arquivos_ok += 1
                    print(f"[RF Import] Arquivo {idx}: {n:,} registros de {url}")
                except Exception as ex:
                    print(f"[RF Import] Falha {url[:60]}: {ex}")
                    continue  # tenta próxima URL do mesmo índice

        if arquivos_ok == 0:
            _rf_import_status["estado"] = "erro"
            _rf_import_status["erro"] = (
                "Nenhum arquivo processado. Servidores da RF inacessíveis na sua rede. "
                "Baixe os arquivos manualmente e faça upload."
            )
            return

        conn.execute("INSERT OR REPLACE INTO rf_meta VALUES ('importado_em',?)", (datetime.now().isoformat(),))
        conn.commit()
        _rf_import_status["estado"] = "concluido"
        print(f"[RF Import] Concluído — {total:,} registros de {arquivos_ok} arquivo(s)")

    except Exception as ex:
        _rf_import_status["estado"] = "erro"
        _rf_import_status["erro"] = str(ex)
        print(f"[RF Import] Erro: {ex}")
    finally:
        conn.close()


# ─── DADOS RF — BUSCA ─────────────────────────────────────────────────────────

def _buscar_cnpjs_pf(nome: str) -> list[str]:
    """Retorna CNPJs (matrizes) onde a PF com este nome é sócia."""
    try:
        conn = sqlite3.connect(_DB)
        rows = conn.execute(
            "SELECT DISTINCT cnpj_basico FROM socios_rf WHERE nome=? AND tipo='1'",
            (nome.upper(),),
        ).fetchall()
        conn.close()
        return [_basico_to_cnpj_matriz(r[0]) for r in rows]
    except Exception:
        return []

def _buscar_cnpjs_pj(cnpj_socio: str) -> list[str]:
    """Retorna CNPJs (matrizes) onde esta PJ é sócia."""
    cnpj_socio = _limpar(cnpj_socio)
    try:
        conn = sqlite3.connect(_DB)
        rows = conn.execute(
            "SELECT DISTINCT cnpj_basico FROM socios_rf WHERE cpf_cnpj=? AND tipo='2'",
            (cnpj_socio,),
        ).fetchall()
        conn.close()
        return [_basico_to_cnpj_matriz(r[0]) for r in rows]
    except Exception:
        return []


# ─── NODE BUILDER ─────────────────────────────────────────────────────────────

def _is_holding(dados: dict) -> bool:
    cnae = str(dados.get("cnae_fiscal", ""))[:4]
    desc = (dados.get("cnae_fiscal_descricao", "") or "").lower()
    nat  = (dados.get("descricao_natureza_juridica", "") or "").lower()
    kws  = ["holding", "participações", "participacoes", "participação", "participacao"]
    return cnae in _HOLDING_CNAES or any(k in desc for k in kws) or any(k in nat for k in kws)

def _node_pj(cnpj: str, dados: dict, depth: int, via_rf: bool = False) -> dict:
    cnpj = _limpar(cnpj)
    return {
        "id": cnpj,
        "type": "pj",
        "cnpj": cnpj,
        "cnpj_fmt": _fmt(cnpj),
        "razao_social": dados.get("razao_social", ""),
        "nome_fantasia": dados.get("nome_fantasia", ""),
        "situacao": dados.get("descricao_situacao_cadastral", "") or dados.get("situacao_cadastral", ""),
        "cnae_cod": str(dados.get("cnae_fiscal", "")),
        "cnae_desc": dados.get("cnae_fiscal_descricao", ""),
        "natureza": dados.get("descricao_natureza_juridica", "") or dados.get("natureza_juridica", ""),
        "capital": float(dados.get("capital_social", 0) or 0),
        "abertura": dados.get("data_inicio_atividade", ""),
        "uf": dados.get("uf", ""),
        "municipio": dados.get("municipio", ""),
        "logradouro": f"{dados.get('logradouro','') or ''} {dados.get('numero','') or ''}".strip(),
        "cep": dados.get("cep", ""),
        "qsa_count": len(dados.get("qsa", [])),
        "is_holding": _is_holding(dados),
        "depth": depth,
        "via_rf": via_rf,
        "cnaes_sec": [
            {"codigo": str(s.get("codigo", "")), "desc": s.get("descricao", "")}
            for s in (dados.get("cnaes_secundarios") or [])
        ],
    }


# ─── MAPEAMENTO ───────────────────────────────────────────────────────────────

def _mapear_grupo(cnpj_raiz: str, max_depth: int = 3, cnpjs_extras: list[str] | None = None) -> dict:
    """
    Mapeia o grupo econômico a partir de um CNPJ raiz.
    cnpjs_extras: lista de CNPJs adicionais para incluir no mapa (ex: clicados pelo usuário).
    """
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    visited_cnpjs: set[str] = set()
    visited_edges: set[str] = set()

    def _visit(cnpj: str, depth: int, force: bool = False):
        cnpj = _limpar(cnpj)
        if not cnpj or (cnpj in visited_cnpjs and not force) or depth > max_depth:
            return
        visited_cnpjs.add(cnpj)

        dados = _fetch(cnpj)
        if not dados:
            if cnpj not in nodes:
                nodes[cnpj] = {
                    "id": cnpj, "type": "pj", "cnpj": cnpj, "cnpj_fmt": _fmt(cnpj),
                    "razao_social": "Dados não disponíveis", "situacao": "Desconhecida",
                    "cnae_desc": "", "uf": "", "municipio": "", "capital": 0,
                    "depth": depth, "is_holding": False, "qsa_count": 0,
                    "natureza": "", "abertura": "", "cnaes_sec": [], "via_rf": False,
                }
            return

        nodes[cnpj] = _node_pj(cnpj, dados, depth)

        for socio in dados.get("qsa", []):
            nome       = socio.get("nome_socio", "")
            cpf_raw    = socio.get("cpf_cnpj_socio", "")
            cpf_limpo  = _limpar(cpf_raw)
            tipo_id    = socio.get("identificador_de_socio", 1)
            qualif     = socio.get("qualificacao_socio", "")
            pct        = float(socio.get("percentual_capital_social", 0) or 0)

            if tipo_id == 2 and len(cpf_limpo) == 14:
                # Sócio PJ
                sid = cpf_limpo
                if sid not in nodes:
                    nodes[sid] = {
                        "id": sid, "type": "pj", "cnpj": sid, "cnpj_fmt": _fmt(sid),
                        "razao_social": nome, "situacao": "", "cnae_desc": "", "capital": 0,
                        "uf": "", "municipio": "", "natureza": "", "abertura": "",
                        "depth": depth + 1, "is_holding": False, "qsa_count": 0,
                        "cnaes_sec": [], "via_rf": False,
                        "expandivel": True,   # flag: usuário pode clicar para expandir
                    }
                ek = f"{sid}>{cnpj}"
                if ek not in visited_edges:
                    visited_edges.add(ek)
                    edges.append({"from": sid, "to": cnpj, "pct": pct, "qualif": qualif,
                                  "label": f"{pct:.0f}%" if pct else qualif[:25]})
                if depth + 1 <= max_depth:
                    time.sleep(0.2)
                    _visit(sid, depth + 1)
            else:
                # Sócio PF
                sufixo = cpf_limpo[-4:] if len(cpf_limpo) >= 4 else (cpf_limpo or nome[-4:] if len(nome) >= 4 else "0000")
                pf_id  = f"pf_{re.sub(r'[^a-z0-9]','_', nome.lower()[:30])}_{sufixo}"
                if pf_id not in nodes:
                    nodes[pf_id] = {
                        "id": pf_id, "type": "pf", "nome": nome, "cpf": cpf_raw,
                        "depth": depth + 1, "empresas_no_grafo": [],
                    }
                nodes[pf_id].setdefault("empresas_no_grafo", []).append(cnpj)
                ek = f"{pf_id}>{cnpj}"
                if ek not in visited_edges:
                    visited_edges.add(ek)
                    edges.append({"from": pf_id, "to": cnpj, "pct": pct, "qualif": qualif,
                                  "label": f"{pct:.0f}%" if pct else qualif[:25]})

    # ── Visita principal ──
    _visit(cnpj_raiz, 0)

    # ── CNPJs extras (expansão manual por clique) ──
    for cnpj_extra in (cnpjs_extras or []):
        _visit(cnpj_extra, 1, force=True)

    # ── Enriquecer PJs pendentes ──
    for nid, node in list(nodes.items()):
        if node.get("type") == "pj" and not node.get("cnae_desc") and nid not in visited_cnpjs:
            d = _fetch(nid)
            if d:
                nodes[nid] = _node_pj(nid, d, node.get("depth", 0))

    # ── Busca ampliada via dados RF (somente se importados) ──
    if _rf_disponivel():
        rf_cnpjs_adicionados: set[str] = set()

        # PF → outras empresas via RF
        for node in list(nodes.values()):
            if node.get("type") != "pf":
                continue
            nome = node.get("nome", "")
            if not nome:
                continue
            for cnpj_rf in _buscar_cnpjs_pf(nome):
                c = _limpar(cnpj_rf)
                if c in visited_cnpjs or c in rf_cnpjs_adicionados:
                    continue
                rf_cnpjs_adicionados.add(c)
                d = _fetch(c)
                if not d:
                    continue
                nodes[c] = _node_pj(c, d, node.get("depth", 1) + 1, via_rf=True)
                visited_cnpjs.add(c)
                ek = f"{node['id']}>{c}"
                if ek not in visited_edges:
                    visited_edges.add(ek)
                    edges.append({"from": node["id"], "to": c, "pct": 0,
                                  "qualif": "Sócio (RF)", "label": "RF", "via_rf": True})
                time.sleep(0.15)

        # PJ → outras empresas onde é sócia via RF
        for node in list(nodes.values()):
            if node.get("type") != "pj":
                continue
            cnpj_socio = node.get("cnpj", "")
            if not cnpj_socio:
                continue
            for cnpj_rf in _buscar_cnpjs_pj(cnpj_socio):
                c = _limpar(cnpj_rf)
                if c in visited_cnpjs or c in rf_cnpjs_adicionados:
                    continue
                rf_cnpjs_adicionados.add(c)
                d = _fetch(c)
                if not d:
                    continue
                nodes[c] = _node_pj(c, d, node.get("depth", 0) + 1, via_rf=True)
                visited_cnpjs.add(c)
                ek = f"{cnpj_socio}>{c}"
                if ek not in visited_edges:
                    visited_edges.add(ek)
                    edges.append({"from": cnpj_socio, "to": c, "pct": 0,
                                  "qualif": "Sócio (RF)", "label": "RF", "via_rf": True})
                time.sleep(0.15)

    # ── Alertas ──
    alertas = []
    for n in nodes.values():
        if n.get("type") == "pf":
            cnt = len(n.get("empresas_no_grafo", []))
            if cnt >= 2:
                alertas.append({"tipo": "warning", "titulo": "Sócio com múltiplas empresas",
                                 "msg": f"{n['nome']} é sócio(a) em {cnt} empresa(s) no grupo."})

    for n in nodes.values():
        if n.get("type") == "pj" and n.get("situacao"):
            sit = n["situacao"].upper()
            if any(k in sit for k in ["BAIXADA", "INAPTA", "CANCELADA", "NULA", "SUSPENSA"]):
                alertas.append({"tipo": "danger", "titulo": "Empresa inativa no grupo",
                                 "msg": f"{n.get('razao_social', n['cnpj'])} — {n['situacao']}"})

    for n in nodes.values():
        if n.get("type") == "pj" and n.get("is_holding"):
            alertas.append({"tipo": "info", "titulo": "Holding identificada",
                             "msg": f"{n.get('razao_social','')} — CNAE {n.get('cnae_cod','')}."})

    pf_cnaes: dict[str, set] = {}
    for edge in edges:
        fn = nodes.get(edge["from"], {})
        tn = nodes.get(edge["to"], {})
        if fn.get("type") == "pf":
            pf_cnaes.setdefault(edge["from"], set()).add(tn.get("cnae_cod", "")[:2])
    for pf_id, cnaes_set in pf_cnaes.items():
        if len(cnaes_set) >= 3:
            pf_node = nodes.get(pf_id, {})
            alertas.append({"tipo": "warning", "titulo": "Diversificação setorial acentuada",
                             "msg": f"{pf_node.get('nome',pf_id)} atua em {len(cnaes_set)} segmentos CNAE."})

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
        "rf_disponivel": _rf_disponivel(),
    }


# ─── ENDPOINTS ────────────────────────────────────────────────────────────────

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
        # Suporte a CNPJs extras (expansão por clique no frontend)
        extras_raw = request.args.get("extras", "")
        extras = [_limpar(c) for c in extras_raw.split(",") if _limpar(c) and len(_limpar(c)) == 14]

        grafo = _mapear_grupo(cnpj_limpo, max_depth=profundidade, cnpjs_extras=extras)

        conn = sqlite3.connect(_DB)
        conn.execute("INSERT INTO historico VALUES (?,?,?,?,?,?)",
                     (str(uuid.uuid4())[:8], cnpj_limpo, grafo["razao_social_raiz"],
                      grafo["total_empresas"], grafo["total_pf"], datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return jsonify(grafo)
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


@bp.route("/expandir/<cnpj_alvo>")
def expandir(cnpj_alvo):
    """Retorna os dados de um único CNPJ (PJ sócio) para expansão no grafo."""
    cnpj_limpo = _limpar(cnpj_alvo)
    if len(cnpj_limpo) != 14:
        return jsonify({"error": "CNPJ inválido"}), 400
    dados = _fetch(cnpj_limpo)
    if not dados:
        return jsonify({"error": "CNPJ não encontrado"}), 404

    node = _node_pj(cnpj_limpo, dados, 0)
    qsa_nodes = []
    qsa_edges = []
    visited_edges: set[str] = set()

    for socio in dados.get("qsa", []):
        nome      = socio.get("nome_socio", "")
        cpf_raw   = socio.get("cpf_cnpj_socio", "")
        cpf_limpo = _limpar(cpf_raw)
        tipo_id   = socio.get("identificador_de_socio", 1)
        qualif    = socio.get("qualificacao_socio", "")
        pct       = float(socio.get("percentual_capital_social", 0) or 0)

        if tipo_id == 2 and len(cpf_limpo) == 14:
            sid = cpf_limpo
            qsa_nodes.append({
                "id": sid, "type": "pj", "cnpj": sid, "cnpj_fmt": _fmt(sid),
                "razao_social": nome, "expandivel": True,
                "situacao": "", "cnae_desc": "", "uf": "", "capital": 0,
                "depth": 1, "is_holding": False, "qsa_count": 0,
                "natureza": "", "abertura": "", "via_rf": False, "cnaes_sec": [],
            })
            ek = f"{sid}>{cnpj_limpo}"
            if ek not in visited_edges:
                visited_edges.add(ek)
                qsa_edges.append({"from": sid, "to": cnpj_limpo, "pct": pct, "qualif": qualif,
                                   "label": f"{pct:.0f}%" if pct else qualif[:25]})
        else:
            sufixo = cpf_limpo[-4:] if len(cpf_limpo) >= 4 else "0000"
            pf_id  = f"pf_{re.sub(r'[^a-z0-9]','_', nome.lower()[:30])}_{sufixo}"
            qsa_nodes.append({
                "id": pf_id, "type": "pf", "nome": nome, "cpf": cpf_raw, "depth": 1,
                "empresas_no_grafo": [cnpj_limpo],
            })
            ek = f"{pf_id}>{cnpj_limpo}"
            if ek not in visited_edges:
                visited_edges.add(ek)
                qsa_edges.append({"from": pf_id, "to": cnpj_limpo, "pct": pct, "qualif": qualif,
                                   "label": f"{pct:.0f}%" if pct else qualif[:25]})

    return jsonify({"node": node, "nodes": qsa_nodes, "edges": qsa_edges})


@bp.route("/filiais/<cnpj>")
def filiais(cnpj):
    """
    Busca todas as filiais de um CNPJ enumerando cnpj_ordem 0002, 0003...
    Para após 5 erros consecutivos (sem mais filiais).
    """
    cnpj_limpo = _limpar(cnpj)
    if len(cnpj_limpo) != 14:
        return jsonify({"error": "CNPJ inválido"}), 400

    max_filiais = min(int(request.args.get("max", 300)), 500)
    cnpj_basico = cnpj_limpo[:8]

    # Dados da matriz primeiro
    dados_matriz = _fetch(cnpj_limpo)
    matriz = _node_pj(cnpj_limpo, dados_matriz, 0) if dados_matriz else None

    filiais_encontradas = []
    erros_consecutivos = 0

    for ordem in range(2, max_filiais + 2):
        cnpj_ordem = str(ordem).zfill(4)
        base12 = cnpj_basico + cnpj_ordem
        cnpj_filial = base12 + _cnpj_dv(base12)

        dados = _fetch(cnpj_filial)
        if dados:
            node = _node_pj(cnpj_filial, dados, 1)
            node["cnpj_ordem"] = cnpj_ordem
            filiais_encontradas.append(node)
            erros_consecutivos = 0
        else:
            erros_consecutivos += 1
            if erros_consecutivos >= 5:
                break
        time.sleep(0.15)

    return jsonify({
        "cnpj_raiz": cnpj_limpo,
        "razao_social": dados_matriz.get("razao_social", "") if dados_matriz else "",
        "matriz": matriz,
        "filiais": filiais_encontradas,
        "total_filiais": len(filiais_encontradas),
        "total_estabelecimentos": len(filiais_encontradas) + (1 if matriz else 0),
    })


@bp.route("/historico")
def historico():
    conn = sqlite3.connect(_DB)
    rows = conn.execute(
        "SELECT id, cnpj, razao_social, total_empresas, total_pf, ts "
        "FROM historico ORDER BY ts DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return jsonify([{"id": r[0], "cnpj": r[1], "razao_social": r[2],
                     "total_empresas": r[3], "total_pf": r[4], "ts": r[5]} for r in rows])


@bp.route("/status-rf")
def status_rf():
    total = _rf_total_registros()
    organico = False
    try:
        conn = sqlite3.connect(_DB)
        row = conn.execute("SELECT valor FROM rf_meta WHERE chave='organico_em'").fetchone()
        conn.close()
        organico = bool(row)
    except Exception:
        pass
    return jsonify({
        "disponivel": total > 0,
        "importado_em": _rf_importado_em(),
        "total_registros": total,
        "organico": organico,
        "import_status": _rf_import_status,
        "urls_disponiveis": [u for u in _rf_socios_urls()[:10]],
    })


@bp.route("/importar-rf", methods=["POST"])
def importar_rf():
    global _rf_import_status
    if _rf_import_status.get("estado") == "importando":
        return jsonify({"error": "Importação já em andamento"}), 400

    arquivos_locais = None

    # Suporte a upload de arquivos ZIP diretamente
    if request.files:
        import tempfile
        tmp_paths = []
        for key in request.files:
            f = request.files[key]
            if not f.filename.lower().endswith(".zip"):
                continue
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip", dir=str(_BASE_DIR))
            f.save(tmp.name)
            tmp.close()
            tmp_paths.append(tmp.name)
        if tmp_paths:
            arquivos_locais = tmp_paths

    t = threading.Thread(target=_importar_rf_thread, args=(arquivos_locais,), daemon=True)
    t.start()
    fonte = "arquivos enviados" if arquivos_locais else "servidores RF"
    return jsonify({"message": f"Importação iniciada — fonte: {fonte}"})
