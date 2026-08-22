FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SPENDALERT_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY *.py ./
COPY scripts/ ./scripts/

RUN useradd --create-home --uid 10001 spendalert \
    && mkdir --parents /data \
    && chown --recursive spendalert:spendalert /app /data

USER spendalert

VOLUME ["/data"]
CMD ["python", "bot.py"]

