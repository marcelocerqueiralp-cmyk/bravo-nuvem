from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3, asyncio, uuid, logging, csv, io, json, re, os, shutil, glob
from datetime import datetime
import threading, time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("consigplat")

app = FastAPI(title="ConsigPlat API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

import pathlib
# Banco fica em C:\consigplat\data\ — pasta separada que nunca e sobrescrita
DATA_DIR = pathlib.Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB = str(DATA_DIR / "consigplat.db")
PASTA_IN  = "importacao"          # pasta monitorada — jogue planilhas aqui
PASTA_OK  = "importacao/ok"       # arquivos processados com sucesso
PASTA_ERR = "importacao/erro"     # arquivos com problema

for p in [PASTA_IN, PASTA_OK, PASTA_ERR]:
    os.makedirs(p, exist_ok=True)

# ─────────────────────────────────────────────
# Banco de dados
# ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cpf TEXT UNIQUE NOT NULL,
            nome TEXT, nasc TEXT, nb TEXT, especie TEXT, situacao TEXT,
            criado TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS consultas (
            id TEXT PRIMARY KEY, cpf TEXT NOT NULL, fonte TEXT,
            banco TEXT, competencia TEXT,
            margem_livre REAL, margem_util REAL, margem_total REAL,
            status TEXT DEFAULT 'pendente', erro TEXT, operador TEXT,
            criado TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS webhook_payloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cpf TEXT, origem TEXT, payload TEXT,
            criado TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS base_servidores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cpf TEXT UNIQUE NOT NULL,
            dados TEXT NOT NULL,
            importado_em TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS colunas_cadastradas (
            nome TEXT PRIMARY KEY,
            label TEXT,
            tipo TEXT DEFAULT 'texto',
            ordem INTEGER DEFAULT 99,
            criado TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS historico_importacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            arquivo TEXT,
            total INTEGER,
            inseridos INTEGER,
            atualizados INTEGER,
            erros INTEGER,
            novas_colunas TEXT,
            status TEXT,
            criado TEXT DEFAULT (datetime('now','localtime'))
        );
        """)
        colunas_fixas = [
            ("CPF","CPF","cpf",1),("SERVIDOR","Nome","texto",2),
            ("MATRICULA","Matricula","texto",3),("SECRETARIA","Secretaria","texto",4),
            ("LOTACAO","Lotacao","texto",5),("SITUACAO","Situacao","texto",6),
            ("TIPO_SERVIDOR","Tipo Servidor","texto",7),
            ("MARGEM_DISPONIVEL","Margem Disponivel","valor",8),
            ("MARGEM_REAL","Margem Real","valor",9),
            ("MARGEM_TOTAL","Margem Total","valor",10),
            ("MARG_DISP_TABELA","Marg. Disp. Tab.","valor",11),
            ("PCT_MARGEM","Pct Margem","perc",12),
            ("QTD_DESCONTO","Qtd Descontos","numero",13),
            ("VALOR_DESCONTO","Valor Desconto","valor",14),
            ("DESCONTOS","Descontos","valor",15),
            ("VD","VD","valor",16),("VD_DESCONTO","VD Desconto","valor",17),
            ("VERBA_DESCONTO","Verba Desconto","texto",18),
            ("MOTIVO_SITUACAO","Motivo Situacao","texto",19),
            ("SIT_DESCONTO","Sit. Desconto","texto",20),
        ]
        for n,l,t,o in colunas_fixas:
            conn.execute("INSERT OR IGNORE INTO colunas_cadastradas (nome,label,tipo,ordem) VALUES (?,?,?,?)",(n,l,t,o))
    log.info("Banco iniciado.")

init_db()
_job_status = {}

@app.on_event("startup")
async def startup_event():
    """Analisa oportunidades ao iniciar o servidor."""
    await asyncio.sleep(2)
    analisar_oportunidades()
    log.info("Analise inicial de oportunidades concluida.")

# ─────────────────────────────────────────────
# Utilitarios de limpeza
# ─────────────────────────────────────────────
def limpar_cpf(raw: str) -> str:
    if not raw: return ""
    s = str(raw).strip()
    if re.search(r'[eE]', s):
        try:
            s = str(int(round(float(s.replace(",",".")))))
        except: return ""
    digits = re.sub(r'\D', '', s)
    if len(digits) > 11: digits = digits[-11:]
    return digits.zfill(11) if len(digits) >= 7 else ""

def limpar_valor(v) -> float:
    if not v or str(v).strip() == "": return 0.0
    s = re.sub(r'[R$\s]', '', str(v).strip())
    s = re.sub(r'[^\d,.\-]', '', s)
    if ',' in s and '.' in s:
        s = s.replace('.','').replace(',','.')
    elif ',' in s:
        s = s.replace(',','.')
    try: return float(s)
    except: return 0.0

def detectar_separador(texto: str) -> str:
    linha = texto.split("\n")[0]
    if "\t" in linha: return "\t"
    if ";" in linha: return ";"
    return ","

def detectar_coluna_cpf(cols: list):
    for c in cols:
        if c and "cpf" in c.strip().lower(): return c
    return None

def tipo_col(nome: str) -> str:
    n = nome.upper()
    if any(k in n for k in ["VALOR","MARGEM","VD","DESCONTO","REAL","TOTAL","DISP","TAB"]): return "valor"
    if "PCT" in n or "PERCENT" in n: return "perc"
    if "QTD" in n or "QUANT" in n: return "numero"
    return "texto"

def ler_arquivo(path: str) -> str:
    for enc in ["utf-8-sig","latin-1","cp1252","utf-8"]:
        try:
            with open(path, "rb") as f:
                return f.read().decode(enc)
        except: continue
    raise ValueError("Encoding nao reconhecido")

# ─────────────────────────────────────────────
# Motor de importacao (reutilizado por API e agendador)
# ─────────────────────────────────────────────
def processar_texto(text: str, nome_arquivo: str = "upload") -> dict:
    sep = detectar_separador(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=sep)
    cabecalho = reader.fieldnames or []
    col_cpf = detectar_coluna_cpf(cabecalho)
    if not col_cpf:
        raise ValueError("Coluna CPF nao encontrada em: " + str(cabecalho[:10]))

    inseridos = atualizados = erros = 0
    novas_colunas = []
    rows = list(reader)

    with get_db() as conn:
        existentes = {r["nome"] for r in conn.execute("SELECT nome FROM colunas_cadastradas").fetchall()}
        ordem = conn.execute("SELECT MAX(ordem) as m FROM colunas_cadastradas").fetchone()["m"] or 20
        for col in cabecalho:
            if not col: continue
            cu = col.strip().upper()
            if cu not in existentes:
                ordem += 1
                conn.execute("INSERT OR IGNORE INTO colunas_cadastradas (nome,label,tipo,ordem) VALUES (?,?,?,?)",
                             (cu, col.strip().title().replace("_"," "), tipo_col(cu), ordem))
                novas_colunas.append(col.strip())
                log.info(f"Nova coluna: {col.strip()}")

        for row in rows:
            try:
                cpf = limpar_cpf(row.get(col_cpf,""))
                if not cpf: erros += 1; continue
                dados = {k.strip().upper(): str(v).strip() for k,v in row.items() if k}
                dados["CPF"] = cpf
                jstr = json.dumps(dados, ensure_ascii=False)
                if conn.execute("SELECT id FROM base_servidores WHERE cpf=?", (cpf,)).fetchone():
                    conn.execute("UPDATE base_servidores SET dados=?, importado_em=datetime('now','localtime') WHERE cpf=?",
                                 (jstr, cpf))
                    atualizados += 1
                else:
                    conn.execute("INSERT INTO base_servidores (cpf,dados) VALUES (?,?)", (cpf, jstr))
                    inseridos += 1
            except Exception as e:
                erros += 1
                log.warning(f"Linha ignorada: {e}")

        conn.execute("""INSERT INTO historico_importacoes
            (arquivo,total,inseridos,atualizados,erros,novas_colunas,status)
            VALUES (?,?,?,?,?,?,?)""",
            (nome_arquivo, len(rows), inseridos, atualizados, erros,
             json.dumps(novas_colunas), "ok"))

    msg = f"{inseridos} inseridos, {atualizados} atualizados."
    if erros: msg += f" {erros} ignorados."
    if novas_colunas: msg += f" Novas colunas: {', '.join(novas_colunas)}."
    log.info(f"[IMPORT] {nome_arquivo}: {msg}")
    return {"ok":True, "inseridos":inseridos, "atualizados":atualizados,
            "erros":erros, "novas_colunas":novas_colunas, "mensagem":msg}

# ─────────────────────────────────────────────
# Agendador: monitora pasta e importa automaticamente
# ─────────────────────────────────────────────
_ultima_verificacao = None
_arquivos_importados_hoje = []

# Estado das oportunidades
_oportunidades_cache = {"quente": 0, "morno": 0, "frio": 0, "refin": 0, "livre": 0, "total": 0, "atualizado": None}
_alertas_pendentes = []

def analisar_oportunidades():
    """Analisa a base e classifica oportunidades automaticamente."""
    global _oportunidades_cache, _alertas_pendentes
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT dados FROM base_servidores").fetchall()
        quente = morno = frio = sem = refin = livre = 0
        for row in rows:
            try:
                r = json.loads(row["dados"])
                marg = limpar_valor(r.get("MARGEM_DISPONIVEL", 0))
                sit = (r.get("SITUACAO") or "").upper()
                qtd = int(r.get("QTD_DESCONTO") or 0)
                is_ativo = "ATIVO" in sit and "OBIT" not in sit and "SUSPEN" not in sit and "DEMIT" not in sit
                if not is_ativo:
                    sem += 1
                    continue
                if marg >= 300: quente += 1
                elif marg >= 50: morno += 1
                elif marg > 0: frio += 1
                else: sem += 1
                if qtd > 0: refin += 1
                if qtd == 0 and marg > 0: livre += 1
            except: pass
        anterior = _oportunidades_cache.copy()
        _oportunidades_cache = {
            "quente": quente, "morno": morno, "frio": frio,
            "sem": sem, "refin": refin, "livre": livre,
            "total": quente + morno + frio,
            "atualizado": datetime.now().strftime("%d/%m/%Y %H:%M")
        }
        # Detecta novos quentes
        if anterior.get("quente", 0) > 0 and quente > anterior.get("quente", 0):
            novos = quente - anterior.get("quente", 0)
            alerta = f"Novas oportunidades QUENTES: +{novos} clientes com margem acima de R$300!"
            _alertas_pendentes.append({"tipo": "quente", "msg": alerta, "ts": datetime.now().strftime("%H:%M")})
            log.info(f"[OPORTUNIDADE] {alerta}")
        log.info(f"[OPORTUNIDADE] Quente:{quente} Morno:{morno} Frio:{frio} Refin:{refin} Livre:{livre}")
    except Exception as e:
        log.error(f"Erro ao analisar oportunidades: {e}")

def verificar_pasta_importacao():
    global _ultima_verificacao, _arquivos_importados_hoje
    extensoes = ["*.csv","*.txt","*.tsv"]
    arquivos = []
    for ext in extensoes:
        arquivos += glob.glob(os.path.join(PASTA_IN, ext))

    for path in arquivos:
        nome = os.path.basename(path)
        log.info(f"[AGENDADOR] Arquivo detectado: {nome}")
        try:
            text = ler_arquivo(path)
            result = processar_texto(text, nome)
            destino = os.path.join(PASTA_OK, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{nome}")
            shutil.move(path, destino)
            _arquivos_importados_hoje.append({
                "arquivo": nome, "resultado": result["mensagem"],
                "hora": datetime.now().strftime("%H:%M:%S")
            })
            log.info(f"[AGENDADOR] OK: {nome} -> {result['mensagem']}")
            analisar_oportunidades()
        except Exception as e:
            destino = os.path.join(PASTA_ERR, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{nome}")
            shutil.move(path, destino)
            log.error(f"[AGENDADOR] ERRO em {nome}: {e}")
            with get_db() as conn:
                conn.execute("INSERT INTO historico_importacoes (arquivo,total,inseridos,atualizados,erros,status) VALUES (?,0,0,0,0,?)",
                             (nome, f"erro: {e}"))

    _ultima_verificacao = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

def loop_agendador():
    """Roda em thread separada. Verifica a pasta a cada 60s e importa o que encontrar."""
    log.info("[AGENDADOR] Iniciado — verificando pasta 'importacao/' a cada 60s")
    while True:
        try:
            verificar_pasta_importacao()
        except Exception as e:
            log.error(f"[AGENDADOR] Erro geral: {e}")
        time.sleep(60)

# Inicia o agendador em background
_thread_agendador = threading.Thread(target=loop_agendador, daemon=True)
_thread_agendador.start()

# ─────────────────────────────────────────────
# Bot de consulta
# ─────────────────────────────────────────────
class ConsultaRequest(BaseModel):
    cpf: str
    operador: Optional[str] = "sistema"

class WebhookPayload(BaseModel):
    origem: str
    cpf: str
    dados: dict


# ─────────────────────────────────────────────
# Sistema de multiplas contas ConsigLog
# ─────────────────────────────────────────────

# Contas configuradas (salvas em arquivo local)
def carregar_contas():
    cfg_path = DATA_DIR / "contas.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))
        except: pass
    return [
        {"id": 1, "nome": "BA-SAEB",   "convenio": "saeb",   "usuario": "", "senha": "", "ativo": True},
        {"id": 2, "nome": "BA-SUPREV", "convenio": "suprev", "usuario": "", "senha": "", "ativo": True},
    ]

def salvar_contas(contas):
    cfg_path = DATA_DIR / "contas.json"
    cfg_path.write_text(json.dumps(contas, ensure_ascii=False, indent=2), encoding="utf-8")

async def rodar_bot_conta(job_id, cpf, operador, conta):
    """Roda o bot para uma conta especifica."""
    conta_id = conta["id"]
    nome = conta["nome"]
    _job_status[job_id]["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] [{nome}] Iniciando consulta CPF: {cpf}")

    def push(msg):
        _job_status[job_id]["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] [{nome}] {msg}")

    try:
        # Usa o bot real do ConsigLog
        try:
            from consiglog_bot import consultar_margem_conta
            r = await consultar_margem_conta(cpf, conta, push)
            resultado = {
                "conta_id":    conta_id,
                "convenio":    nome,
                "margem_livre": r.get("margem_livre", 0),
                "margem_util":  r.get("margem_util", 0),
                "margem_total": r.get("margem_total", 0),
                "servidor":     r.get("servidor", ""),
                "situacao":     r.get("situacao", ""),
                "status":       r.get("status", "erro"),
            }
        except ImportError:
            push("consiglog_bot.py nao encontrado — usando base importada")
            await asyncio.sleep(0.5)
            with get_db() as conn:
                row = conn.execute("SELECT dados FROM base_servidores WHERE cpf=?", (cpf,)).fetchone()
            if row:
                base = json.loads(row["dados"])
                livre = limpar_valor(base.get("MARGEM_DISPONIVEL", 0))
                util  = limpar_valor(base.get("VALOR_DESCONTO", 0))
                total = limpar_valor(base.get("MARGEM_TOTAL", 0))
                push(f"Base importada: {base.get('SERVIDOR','—')} | R$ {livre:.2f}")
                resultado = {"conta_id": conta_id, "convenio": nome,
                             "margem_livre": livre, "margem_util": util,
                             "margem_total": total, "status": "ok"}
            else:
                push("CPF nao encontrado.")
                resultado = {"conta_id": conta_id, "convenio": nome,
                             "margem_livre": 0, "margem_util": 0,
                             "margem_total": 0, "status": "nao_encontrado"}
        return resultado

    except Exception as e:
        push(f"Erro: {e}")
        return {"conta_id": conta_id, "convenio": nome, "margem_livre": 0, "margem_util": 0, "margem_total": 0, "status": f"erro: {e}"}

async def rodar_bots_paralelo(job_id, cpf, operador):
    """Roda todos os bots ativos em paralelo e consolida os resultados."""
    _job_status[job_id] = {"status": "rodando", "logs": [], "dados": None, "por_convenio": []}

    contas = [c for c in carregar_contas() if c.get("ativo")]
    if not contas:
        _job_status[job_id]["logs"].append("Nenhuma conta ativa configurada.")
        _job_status[job_id]["status"] = "concluido"
        return

    _job_status[job_id]["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Acionando {len(contas)} bot(s) em paralelo...")

    # Roda todos em paralelo
    tarefas = [rodar_bot_conta(job_id, cpf, operador, conta) for conta in contas]
    resultados = await asyncio.gather(*tarefas)

    # Consolida
    margem_livre_total = sum(r["margem_livre"] for r in resultados if r["status"] == "ok")
    margem_util_total  = sum(r["margem_util"]  for r in resultados if r["status"] == "ok")
    margem_total_total = sum(r["margem_total"] for r in resultados if r["status"] == "ok")

    dados_consolidados = {
        "banco": "ConsigLog (SAEB + SUPREV)",
        "competencia": datetime.now().strftime("%m/%Y"),
        "margem_livre": margem_livre_total,
        "margem_util":  margem_util_total,
        "margem_total": margem_total_total,
    }

    # Salva no historico
    with get_db() as conn:
        for r in resultados:
            if r["status"] == "ok":
                jid = str(uuid.uuid4())
                conn.execute("""
                    INSERT OR REPLACE INTO consultas
                    (id,cpf,fonte,banco,competencia,margem_livre,margem_util,margem_total,status,operador)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (jid, cpf, f"bot_{r['convenio'].lower().replace('-','_')}",
                      r["convenio"], dados_consolidados["competencia"],
                      r["margem_livre"], r["margem_util"], r["margem_total"],
                      "ok", operador))

    _job_status[job_id]["status"] = "concluido"
    _job_status[job_id]["dados"] = dados_consolidados
    _job_status[job_id]["por_convenio"] = list(resultados)
    _job_status[job_id]["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Consolidado: Margem livre total R$ {margem_livre_total:.2f}")

async def rodar_bot(job_id, cpf, operador):
    _job_status[job_id] = {"status":"rodando","logs":[],"dados":None}
    def push(m):
        _job_status[job_id]["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {m}")
    push("Consultando CPF: " + cpf)
    await asyncio.sleep(0.2)
    with get_db() as conn:
        row = conn.execute("SELECT dados FROM base_servidores WHERE cpf=?", (cpf,)).fetchone()
    if row:
        base = json.loads(row["dados"])
        livre = limpar_valor(base.get("MARGEM_DISPONIVEL",0))
        util  = limpar_valor(base.get("VALOR_DESCONTO",0))
        total = limpar_valor(base.get("MARGEM_TOTAL",0))
        push(f"Encontrado: {base.get('SERVIDOR','—')}")
        push(f"Margem disponivel: R$ {livre:.2f}")
        dados = {"banco":"ConsigLog BA-SAEB","competencia":datetime.now().strftime("%m/%Y"),
                 "margem_livre":livre,"margem_util":util,"margem_total":total}
        with get_db() as conn:
            conn.execute("""INSERT OR REPLACE INTO consultas
                (id,cpf,fonte,banco,competencia,margem_livre,margem_util,margem_total,status,operador)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (job_id,cpf,"base_importada",dados["banco"],dados["competencia"],livre,util,total,"ok",operador))
        _job_status[job_id].update({"status":"concluido","dados":dados})
        push("Concluido!")
    else:
        push("CPF nao encontrado. Coloque o CSV na pasta 'importacao/' para atualizar a base.")
        _job_status[job_id].update({"status":"concluido","dados":{}})

# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────
@app.get("/")
def root():
    return {"app":"ConsigPlat","status":"online"}

@app.post("/importar-csv")
async def importar_csv(file: UploadFile = File(...)):
    content = await file.read()
    text = None
    for enc in ["utf-8-sig","latin-1","cp1252","utf-8"]:
        try: text = content.decode(enc); break
        except: continue
    if not text: raise HTTPException(400, "Nao foi possivel ler o arquivo.")
    return processar_texto(text, file.filename or "upload")

@app.get("/agendador/status")
def agendador_status():
    return {
        "ultima_verificacao": _ultima_verificacao,
        "arquivos_hoje": _arquivos_importados_hoje,
        "pasta_monitorada": os.path.abspath(PASTA_IN),
        "instrucao": "Coloque qualquer CSV nessa pasta e ele sera importado automaticamente em ate 60 segundos."
    }

@app.post("/agendador/forcar")
def forcar_importacao():
    verificar_pasta_importacao()
    return {"ok":True, "mensagem":"Verificacao forcada. Arquivos processados."}

@app.get("/historico-importacoes")
def historico_importacoes():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM historico_importacoes ORDER BY criado DESC LIMIT 50").fetchall()
    return [dict(r) for r in rows]

@app.get("/colunas")
def listar_colunas():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM colunas_cadastradas ORDER BY ordem").fetchall()
    return [dict(r) for r in rows]

@app.get("/base/todos")
def base_todos():
    """Retorna todos os servidores da base para a tela de Gestao."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT cpf, dados, importado_em FROM base_servidores ORDER BY importado_em DESC"
        ).fetchall()
    resultado = []
    for r in rows:
        try:
            d = json.loads(r["dados"])
            d["_importado_em"] = r["importado_em"]
            # converte valores monetarios
            for c in ["MARGEM_DISPONIVEL","MARGEM_REAL","MARGEM_TOTAL",
                      "MARG_DISP_TABELA","VALOR_DESCONTO","DESCONTOS","VD","VD_DESCONTO"]:
                if c in d:
                    d[c] = limpar_valor(d[c])
            resultado.append(d)
        except:
            pass
    return resultado

@app.get("/base/stats")
def base_stats():
    with get_db() as conn:
        total   = conn.execute("SELECT COUNT(*) as n FROM base_servidores").fetchone()["n"]
        ultima  = conn.execute("SELECT MAX(importado_em) as dt FROM base_servidores").fetchone()["dt"]
        colunas = conn.execute("SELECT COUNT(*) as n FROM colunas_cadastradas").fetchone()["n"]
        imports = conn.execute("SELECT COUNT(*) as n FROM historico_importacoes WHERE status='ok'").fetchone()["n"]
    return {"total":total,"ultima_importacao":ultima,"colunas":colunas,"total_importacoes":imports}

@app.get("/base/buscar/{cpf}")
def base_buscar(cpf: str):
    cpf = limpar_cpf(cpf)
    with get_db() as conn:
        row = conn.execute("SELECT dados,importado_em FROM base_servidores WHERE cpf=?", (cpf,)).fetchone()
    if not row: raise HTTPException(404, "CPF nao encontrado.")
    d = json.loads(row["dados"])
    d["_importado_em"] = row["importado_em"]
    return d

@app.post("/consultar")
async def consultar(req: ConsultaRequest, bg: BackgroundTasks):
    cpf = limpar_cpf(req.cpf)
    if not cpf: raise HTTPException(400, "CPF invalido.")
    job_id = str(uuid.uuid4())
    bg.add_task(rodar_bots_paralelo, job_id, cpf, req.operador or "sistema")
    return {"job_id": job_id}

@app.get("/status/{job_id}")
def status_job(job_id: str):
    if job_id not in _job_status: raise HTTPException(404, "Job nao encontrado.")
    return _job_status[job_id]

@app.get("/cliente/{cpf}")
def buscar_cliente(cpf: str):
    cpf = limpar_cpf(cpf)
    with get_db() as conn:
        cliente  = conn.execute("SELECT * FROM clientes WHERE cpf=?", (cpf,)).fetchone()
        historico= conn.execute("SELECT * FROM consultas WHERE cpf=? ORDER BY criado DESC LIMIT 20", (cpf,)).fetchall()
        base_row = conn.execute("SELECT dados FROM base_servidores WHERE cpf=?", (cpf,)).fetchone()
    base = json.loads(base_row["dados"]) if base_row else None
    if base:
        for c in ["MARGEM_DISPONIVEL","MARGEM_REAL","MARGEM_TOTAL","MARG_DISP_TABELA","VALOR_DESCONTO","DESCONTOS","VD","VD_DESCONTO"]:
            if c in base: base[c] = limpar_valor(base[c])
    return {"cliente":dict(cliente) if cliente else None,
            "historico":[dict(r) for r in historico],"base":base}


@app.get("/contas")
def listar_contas():
    return carregar_contas()

@app.post("/contas")
def salvar_contas_endpoint(contas: list):
    salvar_contas(contas)
    return {"ok": True, "mensagem": f"{len(contas)} contas salvas."}

@app.put("/contas/{conta_id}")
def atualizar_conta(conta_id: int, dados: dict):
    contas = carregar_contas()
    for c in contas:
        if c["id"] == conta_id:
            c.update(dados)
            break
    else:
        contas.append({**dados, "id": conta_id})
    salvar_contas(contas)
    return {"ok": True}

@app.get("/oportunidades")
def get_oportunidades():
    return _oportunidades_cache

@app.get("/oportunidades/alertas")
def get_alertas():
    global _alertas_pendentes
    alertas = _alertas_pendentes.copy()
    _alertas_pendentes = []
    return alertas

@app.post("/oportunidades/analisar")
def forcar_analise():
    analisar_oportunidades()
    return {"ok": True, "dados": _oportunidades_cache}

@app.get("/oportunidades/lista/{tipo}")
def listar_oportunidade(tipo: str):
    """Retorna lista de clientes por tipo de oportunidade."""
    with get_db() as conn:
        rows = conn.execute("SELECT dados FROM base_servidores").fetchall()
    resultado = []
    for row in rows:
        try:
            r = json.loads(row["dados"])
            marg = limpar_valor(r.get("MARGEM_DISPONIVEL", 0))
            sit = (r.get("SITUACAO") or "").upper()
            qtd = int(r.get("QTD_DESCONTO") or 0)
            is_ativo = "ATIVO" in sit and "OBIT" not in sit and "SUSPEN" not in sit
            # Converte valores monetarios
            for c in ["MARGEM_DISPONIVEL","MARGEM_REAL","MARGEM_TOTAL","VALOR_DESCONTO"]:
                if c in r: r[c] = limpar_valor(r[c])
            if tipo == "quente" and is_ativo and marg >= 300: resultado.append(r)
            elif tipo == "morno" and is_ativo and marg >= 50 and marg < 300: resultado.append(r)
            elif tipo == "frio" and is_ativo and marg > 0 and marg < 50: resultado.append(r)
            elif tipo == "sem" and (not is_ativo or marg <= 0): resultado.append(r)
            elif tipo == "refin" and is_ativo and qtd > 0: resultado.append(r)
            elif tipo == "livre" and is_ativo and qtd == 0 and marg > 0: resultado.append(r)
        except: pass
    resultado.sort(key=lambda x: float(x.get("MARGEM_DISPONIVEL", 0)), reverse=True)
    return resultado

# ─── Gerenciamento do Bot WhatsApp ───────────────────────────────────
import subprocess as _subprocess
_whatsapp_proc = None

@app.post("/whatsapp/iniciar")
def iniciar_whatsapp():
    global _whatsapp_proc
    try:
        if _whatsapp_proc and _whatsapp_proc.poll() is None:
            return {"ok": True, "status": "ja_rodando", "mensagem": "Bot ja esta rodando"}
        log_file = open(r"C:\consigplat\logs\whatsapp.log", "a", encoding="utf-8")
        _whatsapp_proc = _subprocess.Popen(
            ["node", r"C:\consigplat\whatsapp\whatsapp_bravo.js"],
            cwd=r"C:\consigplat\whatsapp",
            stdout=log_file,
            stderr=log_file,
            creationflags=_subprocess.CREATE_NO_WINDOW if hasattr(_subprocess, 'CREATE_NO_WINDOW') else 0
        )
        return {"ok": True, "status": "iniciado", "pid": _whatsapp_proc.pid, "mensagem": "Bot WhatsApp iniciado!"}
    except Exception as e:
        return {"ok": False, "mensagem": str(e)}

@app.post("/whatsapp/parar")
def parar_whatsapp():
    global _whatsapp_proc
    try:
        if _whatsapp_proc and _whatsapp_proc.poll() is None:
            _whatsapp_proc.terminate()
            _whatsapp_proc = None
            return {"ok": True, "mensagem": "Bot parado."}
        return {"ok": True, "mensagem": "Bot nao estava rodando."}
    except Exception as e:
        return {"ok": False, "mensagem": str(e)}

@app.get("/whatsapp/status-bot")
def status_whatsapp_bot():
    global _whatsapp_proc
    rodando = _whatsapp_proc is not None and _whatsapp_proc.poll() is None
    # Tenta verificar se API do bot esta respondendo
    import urllib.request
    conectado = False
    try:
        resp = urllib.request.urlopen("http://localhost:3000/status", timeout=2)
        data = json.loads(resp.read())
        conectado = data.get("online") == True or data.get("status") == "online"
    except: pass
    return {
        "processo_rodando": rodando,
        "whatsapp_conectado": conectado,
        "pid": _whatsapp_proc.pid if rodando else None
    }

@app.get("/whatsapp/log")
def log_whatsapp():
    try:
        log_path = r"C:\consigplat\logs\whatsapp.log"
        if not os.path.exists(log_path):
            return {"linhas": []}
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            linhas = f.readlines()
        return {"linhas": [l.strip() for l in linhas[-50:]]}
    except Exception as e:
        return {"linhas": [str(e)]}

@app.get("/webhook/historico/todos")
def historico_webhook_todos():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM webhook_payloads ORDER BY criado DESC LIMIT 50"
        ).fetchall()
    return [dict(r) for r in rows]

@app.post("/webhook")
def receber_webhook(payload: WebhookPayload):
    cpf = limpar_cpf(payload.cpf)
    with get_db() as conn:
        conn.execute("INSERT INTO webhook_payloads (cpf,origem,payload) VALUES (?,?,?)",
                     (cpf,payload.origem,json.dumps(payload.dados)))
    return {"ok":True}

@app.get("/app", response_class=HTMLResponse)
def frontend():
    import pathlib
    html_path = pathlib.Path(__file__).parent / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html nao encontrado</h1>")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
