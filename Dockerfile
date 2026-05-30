# ─── Base: Python 3.11 slim ──────────────────────────────────────────────────
FROM python:3.11-slim

# Instala Node.js 18, Chromium e supervisor
RUN apt-get update && apt-get install -y \
    curl ca-certificates gnupg supervisor \
    chromium chromium-driver \
    fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 \
    libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 \
    libnspr4 libnss3 libx11-xcb1 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libxss1 libxtst6 xdg-utils \
    --no-install-recommends \
  && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
  && apt-get install -y nodejs --no-install-recommends \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# Variáveis de ambiente
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
ENV NODE_ENV=production
ENV PORT=8000

WORKDIR /app

# Python deps primeiro (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Node deps
COPY whatsapp/package.json ./whatsapp/
RUN cd whatsapp && npm install --production --ignore-scripts

# Copia projeto
COPY . .

# Pastas necessárias
RUN mkdir -p /app/data /app/logs /app/whatsapp_session /app/importacao/ok

# Supervisor config
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

EXPOSE 8000

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
