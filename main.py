from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import asyncio, uuid, logging, csv, io, json, re, os, shutil, glob
from datetime import datetime
import threading, time
import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("consigplat")

app = FastAPI(title="ConsigPlat API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATABASE_URL = os.environ.get("DATABASE_URL")
PASTA_IN  = "/tmp/importacao"
PASTA_OK  = "/tmp/importacao/ok"
PASTA_ERR = "/tmp/importacao/erro"

for p in [PASTA_IN, PASTA_OK, PASTA_ERR]:
    os.makedirs(p, exist_ok=True)

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn
    def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS clientes (
                id SERIAL PRIMARY KEY,
                cpf TEXT UNIQUE NOT NULL,
                nome TEXT, nasc TEXT, nb TEXT, especie TEXT, situacao TEXT,
                criado TEXT DEFAULT to_char(now(),'DD/MM/YYYY HH24:MI:SS')
            );
            CREATE TABLE IF NOT EXISTS consultas (
                id TEXT PRIMARY KEY, cpf TEXT NOT NULL, fonte TEXT,
                banco TEXT, competencia TEXT,
                margem_livre REAL, margem_util REAL, margem_total REAL,
                status TEXT DEFAULT 'pendente', erro TEXT, operador TEXT,
                criado TEXT DEFAULT to_char(now(),'DD/MM/YYYY HH24:MI:SS')
            );
            CREATE TABLE IF NOT EXISTS webhook_payloads (
                id SERIAL PRIMARY KEY,
                cpf TEXT, origem TEXT, payload TEXT,
                criado TEXT DEFAULT to_char(now(),'DD/MM/YYYY HH24:MI:SS')
            );
            CREATE TABLE IF NOT EXISTS base_servidores (
                id SERIAL PRIMARY KEY,
                cpf TEXT UNIQUE NOT NULL,
                dados TEXT NOT NULL,
                importado_em TEXT DEFAULT to_char(now(),'DD/MM/YYYY HH24:MI:SS')
            );
            CREATE TABLE IF NOT EXISTS colunas_cadastradas (
                nome TEXT PRIMARY KEY,
                label TEXT,
                tipo TEXT DEFAULT 'texto',
                ordem INTEGER DEFAULT 99,
                criado TEXT DEFAULT to_char(now(),'DD/MM/YYYY HH24:MI:SS')
            );
            CREATE TABLE IF NOT EXISTS historico_importacoes (
                id SERIAL PRIMARY KEY,
                arquivo TEXT,
                total INTEGER,
                inseridos INTEGER,
                atualizados INTEGER,
                erros INTEGER,
                novas_colunas TEXT,
                status TEXT,
                criado TEXT DEFAULT to_char(now(),'DD/MM/YYYY HH24:MI:SS')
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
                cur.execute("INSERT INTO colunas_cadastradas (nome,label,tipo,ordem) VALUES (%s,%s,%s,%s) ON CONFLICT (nome) DO NOTHING",(n,l,t,o))
        conn.commit()
    log.info("Banco iniciado.")

init_db()
_job_status = {}
@app.on_event("startup")
async def startup_event():
    await asyncio.sleep(2)
    analisar_oportunidades()
    log.info("Analise inicial de oportunidades concluida.")

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

_oportunidades_cache = {"quente": 0, "morno": 0, "frio": 0, "refin": 0, "livre": 0, "total": 0, "atualizado": None}
_alertas_pendentes = []

def analisar_oportunidades():
    global _oportunidades_cache, _alertas_pendentes
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT dados FROM base_servidores")
                rows = cur.fetchall()
        quente = morno = frio = sem = refin = livre = 0
        for row in rows:
            try:
                r = json.loads(row["dados"])
                marg = limpar_valor(r.get("MARGEM_DISPONIVEL", 0))
                sit = (r.get("SITUACAO") or "").upper()
                qtd = int(r.get("QTD_DESCONTO") or 0)
                is_ativo = "ATIVO" in sit and "OBIT" not in sit and "SUSPEN" not in sit and "DEMIT" not in sit
                if not is_ativo: sem += 1; continue
                if marg >= 300: quente += 1
                elif marg >= 50: morno += 1
                elif marg > 0: frio += 1
                else: sem += 1
                if qtd > 0: refin += 1
                if qtd == 0 and marg > 0: livre += 1
            except: pass
        _oportunidades_cache = {
            "quente": quente, "morno": morno, "frio": frio,
            "sem": sem, "refin": refin, "livre": livre,
            "total": quente + morno + frio,
            "atualizado": datetime.now().strftime("%d/%m/%Y %H:%M")
        }
    except Exception as e:
        log.error(f"Erro ao analisar oportunidades: {e}")
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
        with conn.cursor() as cur:
            cur.execute("SELECT nome FROM colunas_cadastradas")
            existentes = {r["nome"] for r in cur.fetchall()}
            cur.execute("SELECT MAX(ordem) as m FROM colunas_cadastradas")
            res = cur.fetchone()
            ordem = res["m"] or 20

            for col in cabecalho:
                if not col: continue
                cu = col.strip().upper()
                if cu not in existentes:
                    ordem += 1
                    cur.execute("INSERT INTO colunas_cadastradas (nome,label,tipo,ordem) VALUES (%s,%s,%s,%s) ON CONFLICT (nome) DO NOTHING",
                                (cu, col.strip().title().replace("_"," "), tipo_col(cu), ordem))
                    novas_colunas.append(col.strip())

            for row in rows:
                try:
                    cpf = limpar_cpf(row.get(col_cpf,""))
                    if not cpf: erros += 1; continue
                    dados = {k.strip().upper(): str(v).strip() for k,v in row.items() if k}
                    dados["CPF"] = cpf
                    jstr = json.dumps(dados, ensure_ascii=False)
                    cur.execute("SELECT id FROM base_servidores WHERE cpf=%s", (cpf,))
                    if cur.fetchone():
                        cur.execute("UPDATE base_servidores SET dados=%s, importado_em=to_char(now(),'DD/MM/YYYY HH24:MI:SS') WHERE cpf=%s", (jstr, cpf))
                        atualizados += 1
                    else:
                        cur.execute("INSERT INTO base_servidores (cpf,dados) VALUES (%s,%s)", (cpf, jstr))
                        inseridos += 1
                except Exception as e:
                    erros += 1
                    log.warning(f"Linha ignorada: {e}")

            cur.execute("""INSERT INTO historico_importacoes
                (arquivo,total,inseridos,atualizados,erros,novas_colunas,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (nome_arquivo, len(rows), inseridos, atualizados, erros,
                 json.dumps(novas_colunas), "ok"))
        conn.commit()

    msg = f"{inseridos} inseridos, {atualizados} atualizados."
    if erros: msg += f" {erros} ignorados."
    if novas_colunas: msg += f" Novas colunas: {', '.join(novas_colunas)}."
    return {"ok":True, "inseridos":inseridos, "atualizados":atualizados,
            "erros":erros, "novas_colunas":novas_colunas, "mensagem":msg}
    class ConsultaRequest(BaseModel):
    cpf: str
    operador: Optional[str] = "sistema"

class WebhookPayload(BaseModel):
    origem: str
    cpf: str
    dados: dict

def carregar_contas():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT dados FROM base_servidores WHERE cpf='__contas__'")
                row = cur.fetchone()
                if row:
                    return json.loads(row["dados"])
    except: pass
    return [
        {"id": 1, "nome": "BA-SAEB",   "convenio": "saeb",   "usuario": "", "senha": "", "ativo": True},
        {"id": 2, "nome": "BA-SUPREV", "convenio": "suprev", "usuario": "", "senha": "", "ativo": True},
    ]

def salvar_contas(contas):
    with get_db() as conn:
        with conn.cursor() as cur:
            jstr = json.dumps(contas)
            cur.execute("SELECT id FROM base_servidores WHERE cpf='__contas__'")
            if cur.fetchone():
                cur.execute("UPDATE base_servidores SET dados=%s WHERE cpf='__contas__'", (jstr,))
            else:
                cur.execute("INSERT INTO base_servidores (cpf,dados) VALUES ('__contas__',%s)", (jstr,))
        conn.commit()

async def rodar_bot(job_id, cpf, operador):
    _job_status[job_id] = {"status":"rodando","logs":[],"dados":None}
    def push(m):
        _job_status[job_id]["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {m}")
    push("Consultando CPF: " + cpf)
    await asyncio.sleep(0.2)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT dados FROM base_servidores WHERE cpf=%s", (cpf,))
            row = cur.fetchone()
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
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO consultas
                    (id,cpf,fonte,banco,competencia,margem_livre,margem_util,margem_total,status,operador)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET status=%s""",
                    (job_id,cpf,"base_importada",dados["banco"],dados["competencia"],livre,util,total,"ok",operador,"ok"))
            conn.commit()
        _job_status[job_id].update({"status":"concluido","dados":dados})
        push("Concluido!")
    else:
        push("CPF nao encontrado.")
        _job_status[job_id].update({"status":"concluido","dados":{}})

async def rodar_bots_paralelo(job_id, cpf, operador):
    await rodar_bot(job_id, cpf, operador)
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

@app.get("/historico-importacoes")
def historico_importacoes():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM historico_importacoes ORDER BY criado DESC LIMIT 50")
            rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.get("/colunas")
def listar_colunas():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM colunas_cadastradas ORDER BY ordem")
            rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.get("/base/todos")
def base_todos():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT cpf, dados, importado_em FROM base_servidores WHERE cpf != '__contas__' ORDER BY importado_em DESC")
            rows = cur.fetchall()
    resultado = []
    for r in rows:
        try:
            d = json.loads(r["dados"])
            d["_importado_em"] = r["importado_em"]
            for c in ["MARGEM_DISPONIVEL","MARGEM_REAL","MARGEM_TOTAL","MARG_DISP_TABELA","VALOR_DESCONTO","DESCONTOS","VD","VD_DESCONTO"]:
                if c in d: d[c] = limpar_valor(d[c])
            resultado.append(d)
        except: pass
    return resultado

@app.get("/base/stats")
def base_stats():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) as n FROM base_servidores WHERE cpf != '__contas__'")
            total = cur.fetchone()["n"]
            cur.execute("SELECT MAX(importado_em) as dt FROM base_servidores")
            ultima = cur.fetchone()["dt"]
            cur.execute("SELECT COUNT(*) as n FROM colunas_cadastradas")
            colunas = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) as n FROM historico_importacoes WHERE status='ok'")
            imports = cur.fetchone()["n"]
    return {"total":total,"ultima_importacao":ultima,"colunas":colunas,"total_importacoes":imports}

@app.get("/base/buscar/{cpf}")
def base_buscar(cpf: str):
    cpf = limpar_cpf(cpf)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT dados,importado_em FROM base_servidores WHERE cpf=%s", (cpf,))
            row = cur.fetchone()
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
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM clientes WHERE cpf=%s", (cpf,))
            cliente = cur.fetchone()
            cur.execute("SELECT * FROM consultas WHERE cpf=%s ORDER BY criado DESC LIMIT 20", (cpf,))
            historico = cur.fetchall()
            cur.execute("SELECT dados FROM base_servidores WHERE cpf=%s", (cpf,))
            base_row = cur.fetchone()
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
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT dados FROM base_servidores WHERE cpf != '__contas__'")
            rows = cur.fetchall()
    resultado = []
    for row in rows:
        try:
            r = json.loads(row["dados"])
            marg = limpar_valor(r.get("MARGEM_DISPONIVEL", 0))
            sit = (r.get("SITUACAO") or "").upper()
            qtd = int(r.get("QTD_DESCONTO") or 0)
            is_ativo = "ATIVO" in sit and "OBIT" not in sit and "SUSPEN" not in sit
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

@app.post("/webhook")
def receber_webhook(payload: WebhookPayload):
    cpf = limpar_cpf(payload.cpf)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO webhook_payloads (cpf,origem,payload) VALUES (%s,%s,%s)",
                        (cpf,payload.origem,json.dumps(payload.dados)))
        conn.commit()
    return {"ok":True}

@app.get("/webhook/historico/todos")
def historico_webhook_todos():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM webhook_payloads ORDER BY criado DESC LIMIT 50")
            rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.get("/agendador/status")
def agendador_status():
    return {"ok": True, "mensagem": "Agendador via pasta desativado na nuvem. Use importacao via CSV."}

@app.post("/agendador/forcar")
def forcar_importacao():
    return {"ok": True, "mensagem": "Use importacao via CSV."}

@app.get("/app", response_class=HTMLResponse)
def frontend():
    import pathlib
    html_path = pathlib.Path(__file__).parent / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html nao encontrado</h1>")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
