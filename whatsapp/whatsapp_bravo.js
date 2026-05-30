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

// ─── Banco SQLite (assíncrono) ─────────────────────────────────────────────────
var dbPath = fs.existsSync(DB_PATH) ? DB_PATH : DB_FALLBACK;
var db = new sqlite3.Database(dbPath, function(err) {
  if (err) {
    log('[AVISO] Banco principal não encontrado, usando: ' + DB_FALLBACK);
    db = new sqlite3.Database(DB_FALLBACK);
  } else {
    log('Banco conectado: ' + dbPath);
  }
});

db.run(`CREATE TABLE IF NOT EXISTS conversas_whatsapp (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  telefone  TEXT NOT NULL,
  direcao   TEXT NOT NULL,
  mensagem  TEXT NOT NULL,
  status    TEXT DEFAULT 'enviada',
  data      DATETIME DEFAULT CURRENT_TIMESTAMP
)`);
db.run(`CREATE INDEX IF NOT EXISTS idx_wa_tel ON conversas_whatsapp(telefone)`);

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
var client = new Client({
  authStrategy: new LocalAuth({
    clientId: 'bravo',
    dataPath: SESSION_DIR,
  }),
  puppeteer: {
    headless: true,
    args: ['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage','--disable-gpu'],
  },
});

client.on('qr', function(qr) {
  log('QR Code gerado — aguardando leitura...');
  qrcode.generate(qr, { small: true });
  qrRaw     = qr;
  botStatus = 'qr_pendente';
  QRCode.toDataURL(qr, { width: 256, margin: 2 }, function(err, url) {
    if (!err) { qrImg = url; log('QR imagem OK'); }
  });
});

client.on('loading_screen', function(pct) {
  botStatus = 'carregando';
  log('Carregando... ' + pct + '%');
});

client.on('authenticated', function() {
  log('Autenticado!');
  botStatus = 'autenticando';
  qrRaw = null; qrImg = null;
});

client.on('auth_failure', function(msg) {
  log('[ERRO] Auth: ' + msg);
  botStatus = 'offline';
});

client.on('ready', function() {
  numero    = client.info && client.info.wid ? client.info.wid.user : 'desconhecido';
  botStatus = 'online';
  qrRaw = null; qrImg = null;
  log('Bot ONLINE! Numero: ' + numero);
});

client.on('disconnected', function(reason) {
  log('Desconectado: ' + reason);
  botStatus = 'offline';
  numero = null;
  setTimeout(function() {
    log('Reconectando...');
    client.initialize().catch(function(e) { log('[ERRO] ' + e.message); });
  }, 15000);
});

client.on('message', function(msg) {
  if (msg.isGroupMsg) return;
  var tel   = msg.from.replace('@c.us','').replace('@s.whatsapp.net','');
  var corpo = (msg.body || '').trim();
  log('Recebido de ' + tel + ': ' + corpo.slice(0,60));
  salvar(tel, 'recebida', corpo, 'recebida');

  var pos = ['sim','s','quero','ok','aceito','confirmo','1','yes'];
  if (pos.indexOf(corpo.toLowerCase()) >= 0) {
    var resp = 'Ótimo! 😊 Um consultor entrará em contato em breve.\n_Bravo Consignado_ 🤝';
    msg.reply(resp).then(function() {
      salvar(tel, 'enviada', resp, 'enviada');
    }).catch(function(e) { log('[ERRO] Reply: ' + e.message); });
  }
});

// ─── Express ──────────────────────────────────────────────────────────────────
var app = express();
app.use(cors({ origin: '*', methods: ['GET','POST','OPTIONS'], allowedHeaders: ['Content-Type','Authorization'] }));
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
