FROM python:3.14-slim

ENV HOST=0.0.0.0
ENV PORT=8080
ENV DB_PATH=/data/telegram_webhooks.sqlite3
ENV REQUIRE_DATA_MOUNT=1

WORKDIR /app
COPY app.py /app/app.py

RUN mkdir -p /data

EXPOSE 8080

CMD ["python", "app.py"]
