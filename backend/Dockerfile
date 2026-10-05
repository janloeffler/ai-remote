FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
# Hash-pinned lockfile: pip refuses any package whose hash is not listed (SEC-010).
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY app ./app
ARG BUILD_TIMESTAMP=unknown
ENV BUILD_TIMESTAMP=${BUILD_TIMESTAMP}
ENV DATABASE_PATH=/data/app.db
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
