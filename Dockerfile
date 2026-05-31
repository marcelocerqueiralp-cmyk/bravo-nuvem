FROM python:3.11-slim

RUN apt-get update && apt-get install -y curl ca-certificates gnupg supervisor chromium chromium-driver fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 libx11-xcb1 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxss1 libxtst6 xdg-utils --no-install-recommends && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - && apt-get install -y nodejs --no-install-recommends && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY whatsapp/package.json ./whatsapp/
RUN cd whatsapp && npm install --production --ignore-scripts

COPY . .

RUN mkdir -p /app/data /app/logs /app/whatsapp_session /app/importacao/ok

COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

EXPOSE 8000
ENV PORT=8000

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]