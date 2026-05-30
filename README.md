# ⬡ ConsigPlat — Plataforma Centralizada de Gestão Consignado

## Estrutura do Projeto

```
consigplat/
├── backend/
│   └── main.py            ← API FastAPI (toda a lógica do servidor)
├── bot/
│   └── consiglog_bot.py   ← Robô Playwright com auto-recuperação
├── frontend/
│   └── index.html         ← Dashboard (abre direto no navegador)
├── requirements.txt
└── .env                   ← Credenciais (crie manualmente)
```

---

## 1. Instalação

```bash
# Clone ou extraia o projeto
cd consigplat

# Instale as dependências Python
pip install -r requirements.txt

# Instale o navegador Chromium para o robô
playwright install chromium
```

---

## 2. Configuração (.env)

Crie o arquivo `.env` na raiz do projeto:

```env
CONSIGLOG_URL=https://consiglog.com.br
CONSIGLOG_USER=seu_usuario_aqui
CONSIGLOG_PASS=sua_senha_aqui
BOT_HEADLESS=true
```

---

## 3. Rodando o Backend

```bash
cd backend
python main.py
# Servidor sobe em http://localhost:8000
```

Ou com reload automático:
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

---

## 4. Abrindo o Frontend

Abra o arquivo `frontend/index.html` diretamente no navegador.
Ele já se conecta automaticamente ao backend em `localhost:8000`.

> Se o backend não estiver rodando, o frontend opera em **modo demo**
> com dados simulados para você testar a interface.

---

## 5. Endpoints da API

| Método | Endpoint                    | Descrição                                    |
|--------|-----------------------------|----------------------------------------------|
| POST   | `/consultar`                | Aciona o robô ConsigLog para um CPF          |
| GET    | `/status/{job_id}`          | Acompanha o progresso do robô em tempo real  |
| GET    | `/cliente/{cpf}`            | Retorna dados + histórico de um CPF          |
| POST   | `/cliente`                  | Cadastra/atualiza dados de um cliente        |
| POST   | `/webhook`                  | Recebe dados de robôs externos via POST/JSON |
| GET    | `/webhook/historico/{cpf}`  | Lista payloads recebidos via webhook         |

Documentação interativa: `http://localhost:8000/docs`

---

## 6. Como Integrar Outros Robôs (Webhook)

Qualquer robô externo (INSS scraper, outro banco, etc.) pode enviar dados
para a plataforma com uma chamada POST simples:

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "origem": "meu_inss_bot",
    "cpf": "12345678901",
    "dados": {
      "beneficio": "1234567890",
      "especie": "41",
      "situacao": "Ativa",
      "competencia": "05/2026",
      "margem_livre": 350.00
    }
  }'
```

Em Python:
```python
import requests
requests.post("http://localhost:8000/webhook", json={
    "origem": "meu_inss_bot",
    "cpf": "12345678901",
    "dados": { ... }
})
```

---

## 7. Integrar o Robô Real (consiglog_bot.py)

Abra `bot/consiglog_bot.py` e ajuste os seletores CSS/XPath conforme
o HTML real do ConsigLog. Os pontos a editar estão marcados com `# ⚠`.

Depois, no `backend/main.py`, na função `rodar_bot_consiglog`, descomente
o bloco `# ── AQUI VOCÊ INTEGRA O BOT REAL ──` e remova a simulação.

---

## 8. Stack Tecnológica

| Camada    | Tecnologia              | Por quê                                      |
|-----------|-------------------------|----------------------------------------------|
| Backend   | FastAPI + uvicorn       | Leve, async nativo, docs automáticas         |
| Banco     | SQLite (→ PostgreSQL)   | Zero config para começar; migra fácil        |
| Automação | Playwright (Python)     | Mais estável que Selenium, suporte async     |
| Frontend  | HTML/CSS/JS puro        | Zero dependências, roda em qualquer máquina  |

Para migrar para PostgreSQL:
```python
# main.py — substitua get_db() por:
import psycopg2
def get_db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))
```

---

## 9. Auto-Recuperação do Robô

O robô tenta até **3 vezes** automaticamente em caso de:
- Timeout de página
- Erro de elemento não encontrado
- Exceção genérica do Playwright

Entre cada tentativa ele aguarda 5 segundos e retorna ao menu
`Operacional > Margem > Consulta de Margem` antes de tentar novamente.

O frontend continua responsivo durante toda a execução (background task).
