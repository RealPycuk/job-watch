FROM python:3.12-alpine
RUN adduser -D -u 10002 -h /app app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY jobwatch/ ./jobwatch/
COPY certs/ ./certs/
COPY config.toml .
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER app
CMD ["python", "-m", "jobwatch.app"]
