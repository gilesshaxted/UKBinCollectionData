# Bin Portal API. Lean image: no browser. Councils that need Selenium are reported as
# "needs_browser" unless SELENIUM_URL points at a Selenium grid (see docker-compose.yml).
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=10000 \
    SBD_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
EXPOSE 10000

CMD ["python", "sbd_server.py"]
