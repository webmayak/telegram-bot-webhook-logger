# Telegram Webhook Inbox

Минимальное локальное приложение для приема входящих webhook-запросов от Telegram-ботов, сохранения их в SQLite и просмотра содержимого базы через браузер.

## Настройки

Настройки читаются из `.env`, если файл есть:

```env
VIEWER_HASH=2f8f7d53c1a64e9c8a3f9c51
PORT=8080
DB_PATH=telegram_webhooks.sqlite3
REQUIRE_DATA_MOUNT=0
```

`VIEWER_HASH` задает скрытый путь к просмотру базы:

```text
http://127.0.0.1:8080/view/<VIEWER_HASH>/
```

Корневой URL `/` ничего не показывает.

## Запуск без Docker

Для локального запуска на хосте удобнее указать локальный путь к базе:

```powershell
python app.py --host 127.0.0.1 --port 8080 --db .\telegram_webhooks.sqlite3
```

Или через переменные окружения / `.env`:

```env
VIEWER_HASH=replace-with-random-secret
HOST=127.0.0.1
PORT=8080
DB_PATH=telegram_webhooks.sqlite3
REQUIRE_DATA_MOUNT=0
```

## Запуск в Docker

Собрать и запустить:

```powershell
docker compose up --build -d
```

Проверить статус:

```powershell
docker compose ps
```

Остановить:

```powershell
docker compose down
```

SQLite-база хранится в Docker volume `telegram-webhook-data`, внутри контейнера по пути `/data/telegram_webhooks.sqlite3`. Volume имеет фиксированное имя `telegram-bot-webhook-logger-data`, чтобы база не менялась при смене имени compose-проекта или stack.

В Docker включена защита `REQUIRE_DATA_MOUNT=1`: приложение не стартует, если путь базы не находится внутри явно примонтированного volume/bind mount. Это нужно, чтобы при деплое через Dockerfile или Dokploy база не создавалась тихо внутри одноразовой файловой системы контейнера.

Для Dokploy нужно добавить persistent storage:

```text
container path: /data
```

Если запускаете контейнер вручную, обязательно примонтируйте постоянный volume:

```powershell
docker run -d `
  -p 8080:8080 `
  -e VIEWER_HASH=replace-with-random-secret `
  -e REQUIRE_DATA_MOUNT=1 `
  -v telegram-bot-webhook-logger-data:/data `
  telegram-bot-webhook-logger
```

Не отключайте `REQUIRE_DATA_MOUNT` на сервере, иначе приложение снова сможет стартовать с нестабильной базой внутри контейнера.

Если приложение уже работало в Docker Compose до появления фиксированного имени volume, старая база могла лежать в volume с именем вида `<project>_telegram-webhook-data`, например `telegram_telegram-webhook-data`. Перед обновлением проверьте:

```powershell
docker volume ls
```

Чтобы продолжить использовать уже существующий volume, временно замените имя volume в `docker-compose.yml`:

```yaml
volumes:
  telegram-webhook-data:
    name: existing-volume-name
```

После этого запустите `docker compose up -d`. Так контейнер подключится к старой базе, а не создаст пустую новую.

## Прием вебхуков

Отправляйте Telegram webhook на:

```text
POST http://<host>:8080/webhook/<bot-token>
```

`<bot-token>` сохраняется отдельно, чтобы можно было фильтровать события по боту. Тело запроса сохраняется целиком. Если тело является JSON, оно также сохраняется в нормализованном виде.

Проверка через `curl`:

```powershell
curl.exe -X POST http://127.0.0.1:8080/webhook/test-bot `
  -H "Content-Type: application/json" `
  -d "{\"update_id\":1,\"message\":{\"text\":\"hello\"}}"
```

## Просмотр базы

Откройте URL с хешем из `.env`:

```text
http://127.0.0.1:8080/view/2f8f7d53c1a64e9c8a3f9c51/
```

На странице показаны последние события. По ссылке в колонке `ID` открывается полный payload и headers конкретного запроса.

Можно фильтровать по токену бота и менять лимит записей через форму сверху.

## Структура базы

Таблица `webhook_events`:

- `id`
- `received_at`
- `bot_token`
- `remote_addr`
- `method`
- `path`
- `headers_json`
- `payload_json`
- `raw_body`
- `parse_error`
