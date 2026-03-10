FROM python:3.12-slim

WORKDIR /app

COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
