FROM python:3.12-slim

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright + Chromium
RUN playwright install --with-deps chromium

# Copy app
COPY . .

# Railway sets PORT env var
ENV PORT=8765
EXPOSE 8765

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
