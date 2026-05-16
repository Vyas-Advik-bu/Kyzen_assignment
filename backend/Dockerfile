FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir hatchling

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY app/ app/

RUN mkdir -p outputs

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
