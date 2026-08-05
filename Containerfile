FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

# non-root user (uid 65532 = "nobody" style, matches other nonroot services)
RUN useradd -u 65532 -m dashuser || true
USER dashuser

EXPOSE 8650
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8650"]
