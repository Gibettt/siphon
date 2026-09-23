FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# Copy requirements and install python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ensure Playwright Chromium browser is installed
RUN playwright install chromium

# Copy application files
COPY . .

# Railway sets PORT dynamically
ENV PORT=8765
EXPOSE 8765

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8765}"]
