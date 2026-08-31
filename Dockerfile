FROM python:3.11-slim

# Install nginx, supervisor, and gettext for envsubst
RUN apt-get update && apt-get install -y nginx supervisor gettext-base && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Setup config files
COPY nginx.conf /etc/nginx/nginx.conf.template
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Render passes the port in $PORT, fallback to 10000 if not set
ENV PORT=10000

CMD ["supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
