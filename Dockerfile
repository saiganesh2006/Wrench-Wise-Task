FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 BANKMIND_DB=/tmp/bankmind.db
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Hosts such as Render / Railway / Hugging Face Spaces inject $PORT (Spaces uses 7860)
EXPOSE 8000
CMD ["sh", "-c", "uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
