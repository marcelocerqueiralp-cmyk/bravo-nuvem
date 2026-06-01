/**
 * whatsapp_bravo.js  —  Bravo Consignado CRM
 * Compatível com Node.js 13+
 * Porta: 3000
 */

const { Client, LocalAuth } = require('whatsapp-web.js');
const express  = require('express');
const cors     = require('cors');
const qrcode   = require('qrcode-terminal');
const QRCode   = require('qrcode');
const sqlite3  = require('sqlite3').verbose();
const path     = require('path');
const fs       = require('fs');

// ─── Caminhos ─────────────────────────────────────────────────────────────────
const BASE_DIR    = 'C:\\consigplat';
const DB_PATH     = path.join(BASE_DIR, 'data', 'consigplat.db');
const DB_FALLBACK = path.join(BASE_DIR, 'consigplat.db');
const SESSION_DIR = path.join(BASE_DIR, 'whatsapp_session');
const LOG_PATH    = path.join(BASE_DIR, 'logs', 'whatsapp.log');
const PORT        = 3000;

// ─── Log ──────────────────────────────────────────────────────────────────────
var logLines = [];
function log(msg) {
  var linha = '[' + new Date().toLocaleString('pt-BR') + '] ' + msg;
  console.log(linha);
  logLines.push(linha);
  if (logLines.length > 200) logLines.shift();
  try { fs.appendFileSync(LOG_PATH, linha + '\n'); } catch(e) {}
}

// ─── Banco SQLite (assíncrono e síncrono consolidado) ──────────────────────────
var dbPath = fs.existsSync(DB_PATH) ? DB_PATH : DB_FALLBACK;
log('Conectando ao banco: ' + dbPath);
var db = new sqlite3.Database(dbPath, function(err) {
  if (err) {
    log('[ERRO] Banco: ' + err.message);
  } else {
    log('Banco conectado com sucesso: ' + dbPath);
    // Cria tabela e índice dentro do callback de conexão aberta para evitar condições de corrida
    db.run(`CREATE TABLE IF NOT EXISTS conversas_whatsapp (
      id        INTEGER PRIMARY KEY AUTOINCREMENT,
      telefone  TEXT NOT NULL,
      direcao   TEXT NOT NULL,
      mensagem  TEXT NOT NULL,
      status    TEXT DEFAULT 'enviada',
      data      DATETIME DEFAULT CURRENT_TIMESTAMP
    )`, function(e) { 
      if (e) log('[DB] Erro ao criar tabela: ' + e.message); 
      else log('[DB] Tabela conversas_whatsapp garantida.');
    });
    db.run(`CREATE INDEX IF NOT EXISTS idx_wa_tel ON conversas_whatsapp(telefone)`);
  }
});

function salvar(tel, dir, msg, st) {
  db.run(
    'INSERT INTO conversas_whatsapp (telefone,direcao,mensagem,status) VALUES (?,?,?,?)',
    [tel, dir, msg, st || dir],
    function(err) { if (err) log('[DB] ' + err.message); }
  );
}

function historico(tel, cb) {
  var t = tel.replace(/\D/g,'').slice(-8);
  db.all(
    'SELECT * FROM conversas_whatsapp WHERE telefone LIKE ? ORDER BY data DESC LIMIT 60',
    ['%' + t + '%'],
    function(err, rows) { cb(err ? [] : rows); }
  );
}

function todasConversas(cb) {
  db.all(
    'SELECT * FROM conversas_whatsapp ORDER BY data DESC LIMIT 300',
    function(err, rows) { cb(err ? [] : rows); }
  );
}

function buscarCliente(cpf, cb) {
  var c = cpf.replace(/\D/g,'');
  db.get(
    "SELECT * FROM base_servidores WHERE replace(replace(cpf,'.',''),'-','')=? LIMIT 1",
    [c],
    function(err, row) { cb(row || null); }
  );
}

// ─── Estado global ─────────────────────────────────────────────────────────────
var botStatus = 'offline';
var numero    = null;
var qrRaw     = null;
var qrImg     = null;

// ─── Cliente WhatsApp ──────────────────────────────────────────────────────────
// Caminhos comuns do Chrome no Windows
var chromePaths = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Users\\' + (process.env.USERNAME||'') + '\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe',
];
var chromePath = chromePaths.find(function(p) { return fs.existsSync(p); });
if (chromePath) { log('Chrome encontrado para Puppeteer: ' + chromePath); }
else { log('[AVISO] Chrome nao encontrado nos caminhos padrao — puppeteer vai tentar sozinho'); }

var client = new Client({
  authStrategy: new LocalAuth({
    clientId: 'bravo',
    dataPath: SESSION_DIR,
  }),
  puppeteer: {
    headless: true,
    executablePath: chromePath || undefined,
    args: ['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage','--disable-gpu'],
  },
});

client.on('qr', function(qr) {
  log('QR Code gerado — aguardando leitura...');
  qrcode.generate(qr, { small: true });
  qrRaw     = qr;
  botStatus = 'qr_pendente';
  QRCode.toDataURL(qr, { width: 256, margin: 2 }, function(err, url) {
    if (!err) { qrImg = url; log('QR imagem gerada com sucesso.'); }
  });
});

client.on('loading_screen', function(pct) {
  botStatus = 'carregando';
  log('Carregando tela do WhatsApp Web... ' + pct + '%');
});

client.on('authenticated', function() {
  log('Sessão autenticada!');
  botStatus = 'autenticando';
  qrRaw = null; qrImg = null;
});

client.on('auth_failure', function(msg) {
  log('[ERRO] Falha de autenticação: ' + msg);
  botStatus = 'offline';
});

client.on('ready', function() {
  numero    = client.info && client.info.wid ? client.info.wid.user : 'desconhecido';
  botStatus = 'online';
  qrRaw = null; qrImg = null;
  log('Bot ONLINE e conectado com o número: ' + numero);
});

client.on('disconnected', function(reason) {
  log('Bot desconectado: ' + reason);
  botStatus = 'offline';
  numero = null;
  setTimeout(function() {
    log('Tentando reconectar...');
    client.initialize().catch(function(e) { log('[ERRO RECONEXÃO] ' + e.message); });
  }, 15000);
});

client.on('message', function(msg) {
  if (msg.isGroupMsg) return;
  var tel   = msg.from.replace('@c.us','').replace('@s.whatsapp.net','');
  var corpo = (msg.body || '').trim();
  log('Mensagem recebida de ' + tel + ': ' + corpo.slice(0,60));
  salvar(tel, 'recebida', corpo, 'recebida');

  var pos = ['sim','s','quero','ok','aceito','confirmo','1','yes'];
  if (pos.indexOf(corpo.toLowerCase()) >= 0) {
    var resp = 'Ótimo! 😊 Um consultor entrará em contato em breve.\n_Bravo Consignado_ 🤝';
    msg.reply(resp).then(function() {
      salvar(tel, 'enviada', resp, 'enviada');
      log('Resposta automática de interesse enviada para ' + tel);
    }).catch(function(e) { log('[ERRO] Responder mensagem: ' + e.message); });
  }
});

// ─── Express ──────────────────────────────────────────────────────────────────
var app = express();

// CORS restrito e seguro
const allowedOrigins = [
  'http://localhost:8000',
  'http://127.0.0.1:8000',
  'http://localhost:3000',
  'http://127.0.0.1:3000',
  'https://bravo-nuvem.onrender.com'
];
app.use(cors({
  origin: function(origin, callback) {
    if(!origin) return callback(null, true);
    if(allowedOrigins.indexOf(origin) === -1){
      var msg = 'A política CORS para este site não permite acesso da origem especificada.';
      return callback(new Error(msg), false);
    }
    return callback(null, true);
  },
  methods: ['GET','POST','OPTIONS'],
  allowedHeaders: ['Content-Type','Authorization']
}));

app.options('*', cors());
app.use(express.json({ limit: '5mb' }));

app.get('/status', function(req, res) {
  res.json({ status: botStatus, online: botStatus === 'online', numero: numero });
});

app.get('/qr', function(req, res) {
  if (qrImg || qrRaw) {
    res.json({ ok: true, qr: qrRaw, qr_img: qrImg });
  } else {
    res.json({ ok: false, qr: null, qr_img: null });
  }
});

app.get('/log', function(req, res) {
  res.json({ linhas: logLines.slice(-50) });
});

app.post('/enviar', function(req, res) {
  var tel = req.body.telefone;
  var msg = req.body.mensagem;
  var cpf = req.body.cpf;

  if (botStatus !== 'online') {
    return res.status(503).json({ ok: false, erro: 'Bot offline. Inicie primeiro.' });
  }
  if (!tel) return res.status(400).json({ ok: false, erro: 'telefone obrigatorio' });

  var num = tel.replace(/\D/g,'');
  var jid = (num.length <= 11 ? '55' + num : num) + '@c.us';

  function enviar(texto) {
    client.sendMessage(jid, texto).then(function() {
      salvar(num, 'enviada', texto, 'enviada');
      log('Enviado para ' + num);
      res.json({ ok: true, para: num });
    }).catch(function(e) {
      log('[ERRO] /enviar: ' + e.message);
      res.status(500).json({ ok: false, erro: e.message });
    });
  }

  if (cpf && !msg) {
    buscarCliente(cpf, function(cli) {
      enviar(cli ? montarOferta(cli) : 'Olá! Temos uma oferta de crédito consignado para você. Responda SIM!');
    });
  } else if (msg) {
    enviar(msg);
  } else {
    res.status(400).json({ ok: false, erro: 'mensagem ou cpf obrigatorio' });
  }
});

app.get('/conversas', function(req, res) {
  var tel = req.query.telefone;
  if (tel) {
    historico(tel, function(rows) { res.json(rows); });
  } else {
    todasConversas(function(rows) { res.json(rows); });
  }
});

// Nova rota /leads para recuperar as conversas de interesse capturadas
app.get('/leads', function(req, res) {
  log('[API] Requisitando leads do SQLite...');
  db.all(
    `SELECT DISTINCT telefone, mensagem, data, status FROM conversas_whatsapp 
     WHERE direcao = 'recebida' AND (
       lower(mensagem) = 'sim' OR lower(mensagem) = 's' OR 
       lower(mensagem) = 'quero' OR lower(mensagem) = 'ok' OR 
       lower(mensagem) = 'aceito' OR lower(mensagem) = '1'
     )
     ORDER BY data DESC LIMIT 100`,
    function(err, rows) {
      if (err) {
        log('[ERRO] /leads SQLite: ' + err.message);
        return res.json([]);
      }
      if (!rows || !rows.length) return res.json([]);

      var leads = rows.map(function(row) {
        var telLimpo = row.telefone.replace(/\D/g,'');
        return {
          data: row.data,
          nome: 'Interesse via WhatsApp (' + telLimpo.slice(-4) + ')',
          cpf: 'Consultar Ficha',
          telefone: row.telefone,
          margem: 350.00, // Preenche com uma margem fictícia padrão de interesse quente/morno
          parcelas: 84,
          banco: 'Campanha WhatsApp',
          status: row.status === 'recebida' ? 'aguardando_digitacao' : row.status
        };
      });
      res.json(leads);
    }
  );
});

// ─── Helpers ───────────────────────────────────────────────────────────────────
function montarOferta(c) {
  var dados = c.dados ? JSON.parse(c.dados) : c;
  var nome  = dados.SERVIDOR || dados.nome || 'Servidor';
  var marg  = dados.MARGEM_DISPONIVEL || dados.margem_livre || '';
  var val   = marg ? 'R$ ' + Number(String(marg).replace(',','.')).toLocaleString('pt-BR', {minimumFractionDigits:2}) : 'disponível';
  return 'Olá, *' + nome + '*! 👋\n\n' +
    'Temos uma oferta de crédito consignado para você:\n\n' +
    '✅ Margem: *' + val + '*\n' +
    '📅 Prazo: até *84 meses*\n\n' +
    'Responda *SIM* para saber mais.\n' +
    '_Bravo Consignado_ 🤝';
}

// ─── Start ─────────────────────────────────────────────────────────────────────
fs.mkdirSync(path.join(BASE_DIR, 'logs'), { recursive: true });

app.listen(PORT, '127.0.0.1', function() {
  log('[API] Rodando em http://localhost:' + PORT);
});

log('[BOT] Iniciando Bravo Consignado WhatsApp Bot...');
client.initialize().catch(function(e) { log('[ERRO FATAL] ' + e.message); });
