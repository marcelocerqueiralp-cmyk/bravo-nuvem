# ─────────────────────────────────────────────────────────────────────────────
# Adicione este bloco no seu main.py
# O index.html chama:
#   GET  /whatsapp/status-bot  → { processo_rodando, whatsapp_conectado }
#   POST /whatsapp/iniciar     → { ok, mensagem }
#   POST /whatsapp/parar       → { mensagem }
#   GET  /whatsapp/log         → { linhas: [...] }
# ─────────────────────────────────────────────────────────────────────────────

import subprocess
import psutil
import httpx
import os
import sys
from pathlib import Path
from fastapi import APIRouter

router_wa = APIRouter(prefix="/whatsapp")

WA_DIR     = Path(r"C:\consigplat\whatsapp")
WA_SCRIPT  = WA_DIR / "whatsapp_bravo.js"
WA_LOG     = Path(r"C:\consigplat\logs\whatsapp.log")
WA_URL     = "http://localhost:3000"
NODE_EXE   = "node"   # precisa estar no PATH


def _bot_rodando() -> bool:
    """Verifica se há processo node rodando o whatsapp_bravo.js"""
    for p in psutil.process_iter(['name', 'cmdline']):
        try:
            if 'node' in (p.info['name'] or '').lower():
                cmd = ' '.join(p.info['cmdline'] or [])
                if 'whatsapp_bravo' in cmd:
                    return True
        except Exception:
            pass
    return False


async def _wa_conectado() -> bool:
    """Pergunta ao bot Node.js se o WhatsApp está conectado"""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{WA_URL}/status")
            d = r.json()
            # Considera conectado se online=True OU status não é offline/qr_pendente
            return d.get("online", False) or d.get("status") in ("ready", "autenticando", "carregando")
    except Exception:
        return False


@router_wa.get("/status-bot")
async def status_bot():
    rodando   = _bot_rodando()
    conectado = await _wa_conectado() if rodando else False
    return {
        "processo_rodando":   rodando,
        "whatsapp_conectado": conectado,
    }


@router_wa.post("/iniciar")
async def iniciar_bot():
    if _bot_rodando():
        return {"ok": True, "mensagem": "Bot já está rodando!"}
    try:
        log_out = open(r"C:\consigplat\logs\whatsapp_out.log", "a")
        subprocess.Popen(
            [NODE_EXE, str(WA_SCRIPT)],
            cwd=str(WA_DIR),
            stdout=log_out,
            stderr=log_out,
            creationflags=subprocess.CREATE_NO_WINDOW,  # sem janela CMD
        )
        return {"ok": True, "mensagem": "Bot WhatsApp iniciado!"}
    except Exception as e:
        return {"ok": False, "mensagem": f"Erro ao iniciar: {e}"}


@router_wa.post("/parar")
async def parar_bot():
    parou = 0
    for p in psutil.process_iter(['name', 'cmdline']):
        try:
            if 'node' in (p.info['name'] or '').lower():
                cmd = ' '.join(p.info['cmdline'] or [])
                if 'whatsapp_bravo' in cmd:
                    p.terminate()
                    parou += 1
        except Exception:
            pass
    return {"mensagem": f"Bot parado ({parou} processo(s) encerrado(s))."}


@router_wa.get("/log")
async def log_bot():
    try:
        if WA_LOG.exists():
            linhas = WA_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
            return {"linhas": linhas[-60:]}
    except Exception:
        pass
    return {"linhas": []}


# ─── Como adicionar ao app principal ────────────────────────────────────────
# No seu main.py, certifique-se que existe:
#
#   from fastapi import FastAPI
#   app = FastAPI()
#
# Depois adicione (pode colar logo após criar o app):
#
#   from whatsapp_routes import router_wa
#   app.include_router(router_wa)
#
# Ou simplesmente cole as funções acima diretamente no main.py
# e adicione:   app.include_router(router_wa)
