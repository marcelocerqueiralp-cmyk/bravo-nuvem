"""
Bravo Consig - Robo ConsigLog
Consulta margem no ConsigLog usando Playwright
"""
import asyncio
import logging
import json
import re
from datetime import datetime

log = logging.getLogger("consiglog_bot")

URL_LOGIN    = "https://govbahia.consiglog.com.br/Login.aspx"
URL_CONSULTA = "https://govbahia.consiglog.com.br/Margem/ConsultaMargem.aspx"

MAX_TENTATIVAS = 3
TIMEOUT_MS     = 25000

def limpar_valor(s):
    if not s: return 0.0
    s = re.sub(r'[R$\s]', '', str(s).strip())
    s = re.sub(r'[^\d,.\-]', '', s)
    if ',' in s and '.' in s:
        s = s.replace('.','').replace(',','.')
    elif ',' in s:
        s = s.replace(',','.')
    try: return float(s)
    except: return 0.0

async def consultar_margem_conta(cpf, conta, push_log=None):
    if push_log is None:
        push_log = lambda msg: log.info(msg)

    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    nome     = conta.get("nome", "Bot")
    usuario  = conta.get("usuario", "")
    senha    = conta.get("senha", "")
    convenio = conta.get("convenio", "saeb")

    if not usuario or not senha:
        push_log(f"[{nome}] Credenciais nao configuradas!")
        return {"convenio": nome, "status": "sem_credenciais",
                "margem_livre": 0, "margem_util": 0, "margem_total": 0}

    for tentativa in range(1, MAX_TENTATIVAS + 1):
        push_log(f"[{nome}] Tentativa {tentativa}/{MAX_TENTATIVAS}")
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox","--disable-dev-shm-usage",
                          "--disable-gpu","--no-zygote"]
                )
                page = await browser.new_page()
                page.set_default_timeout(TIMEOUT_MS)

                # LOGIN
                push_log(f"[{nome}] Abrindo login...")
                await page.goto(URL_LOGIN, wait_until="domcontentloaded")
                await page.wait_for_load_state("networkidle")

                push_log(f"[{nome}] Preenchendo usuario e senha...")
                await page.fill('#Login1_UserName', usuario)
                await page.fill('#Login1_Password', senha)
                await page.click('#Login1_LoginButton')
                await page.wait_for_load_state("networkidle")
                push_log(f"[{nome}] Login OK! URL: {page.url}")

                # SELECAO DE CONVENIO
                if "LoginSelecao" in page.url or "Selecao" in page.url:
                    push_log(f"[{nome}] Selecionando {convenio.upper()}...")
                    rows = await page.query_selector_all('table tr')
                    for row in rows:
                        texto = await row.text_content()
                        if convenio.upper() == "SAEB" and "SAEB" in (texto or "").upper():
                            btn = await row.query_selector('input[type="image"], img')
                            if btn:
                                await btn.click()
                                break
                        elif convenio.upper() == "SUPREV" and "SUPREV" in (texto or "").upper():
                            btn = await row.query_selector('input[type="image"], img')
                            if btn:
                                await btn.click()
                                break
                    await page.wait_for_load_state("networkidle")
                    push_log(f"[{nome}] Convenio selecionado! URL: {page.url}")

                # NAVEGAR PARA CONSULTA DE MARGEM
                push_log(f"[{nome}] Navegando para Consulta de Margem...")
                await page.goto(URL_CONSULTA, wait_until="networkidle")
                push_log(f"[{nome}] Pagina carregada: {page.url}")

                # PREENCHER CPF
                push_log(f"[{nome}] Digitando CPF {cpf}...")
                await page.fill('#CPF', cpf)
                await page.click('#Pesquisar')
                await page.wait_for_load_state("networkidle")
                push_log(f"[{nome}] Resultado: {page.url}")

                # EXTRAIR DADOS
                push_log(f"[{nome}] Extraindo dados...")

                async def campo(sel):
                    try:
                        el = await page.query_selector(sel)
                        if el:
                            v = await el.get_attribute('value')
                            return (v or '').strip()
                    except: pass
                    return ''

                servidor    = await campo('#Servidor')
                secretaria  = await campo('#Secretaria')
                matricula   = await campo('#Matricula')
                situacao    = await campo('#Situacao')
                margem_real = await campo('#MargemReal')
                margem_disp = await campo('#MargemDisponivel')

                push_log(f"[{nome}] Servidor: {servidor}")
                push_log(f"[{nome}] Margem Real: {margem_real}")
                push_log(f"[{nome}] Margem Disponivel: {margem_disp}")
                push_log(f"[{nome}] Situacao: {situacao}")

                await browser.close()

                return {
                    "convenio":     nome,
                    "status":       "ok",
                    "servidor":     servidor,
                    "secretaria":   secretaria,
                    "matricula":    matricula,
                    "situacao":     situacao,
                    "margem_livre": limpar_valor(margem_disp),
                    "margem_util":  0.0,
                    "margem_total": limpar_valor(margem_real),
                    "extraido_em":  datetime.now().isoformat(),
                }

        except PWTimeout as e:
            push_log(f"[{nome}] TIMEOUT: {e}")
        except Exception as e:
            push_log(f"[{nome}] ERRO: {type(e).__name__}: {e}")

        if tentativa < MAX_TENTATIVAS:
            push_log(f"[{nome}] Aguardando 5s...")
            await asyncio.sleep(5)

    return {"convenio": nome, "status": "erro",
            "margem_livre": 0, "margem_util": 0, "margem_total": 0}

async def consultar_todos_convenios(cpf, contas, push_log=None):
    if push_log is None:
        push_log = lambda msg: log.info(msg)
    contas_ativas = [c for c in contas if c.get("ativo", True)]
    if not contas_ativas:
        return {"resultados": [], "consolidado": {}}
    push_log(f"Consultando {len(contas_ativas)} convenio(s) em paralelo...")
    tarefas = [consultar_margem_conta(cpf, c, push_log) for c in contas_ativas]
    resultados = await asyncio.gather(*tarefas)
    ok = [r for r in resultados if r.get("status") == "ok"]
    margem_livre = sum(r.get("margem_livre", 0) for r in ok)
    margem_total = sum(r.get("margem_total", 0) for r in ok)
    dados = ok[0] if ok else {}
    return {
        "resultados": list(resultados),
        "consolidado": {
            "servidor":    dados.get("servidor", ""),
            "secretaria":  dados.get("secretaria", ""),
            "matricula":   dados.get("matricula", ""),
            "situacao":    dados.get("situacao", ""),
            "margem_livre":  margem_livre,
            "margem_util":   0.0,
            "margem_total":  margem_total,
            "banco":         " + ".join(r["convenio"] for r in ok),
            "competencia":   datetime.now().strftime("%m/%Y"),
        }
    }

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    cpf = sys.argv[1] if len(sys.argv) > 1 else "02316206590"
    contas = [
        {"nome": "BA-SAEB",   "convenio": "saeb",   "usuario": "cor003911", "senha": "Cred@2026", "ativo": True},
        {"nome": "BA-SUPREV", "convenio": "suprev", "usuario": "cor003911", "senha": "Cred@2026", "ativo": False},
    ]
    resultado = asyncio.run(consultar_todos_convenios(cpf, contas, print))
    print("\n=== RESULTADO ===")
    print(json.dumps(resultado, ensure_ascii=False, indent=2))
