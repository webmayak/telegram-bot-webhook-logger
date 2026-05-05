from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "telegram_webhooks.sqlite3"
DEFAULT_VIEWER_HASH = "change-me"
MAX_BODY_BYTES = 2 * 1024 * 1024


def is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT NOT NULL,
                bot_token TEXT,
                remote_addr TEXT,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                headers_json TEXT NOT NULL,
                payload_json TEXT,
                raw_body TEXT NOT NULL,
                parse_error TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_webhook_events_received_at "
            "ON webhook_events(received_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_webhook_events_bot_token "
            "ON webhook_events(bot_token)"
        )


def decode_mount_path(value: str) -> Path:
    decoded = (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )
    return Path(decoded)


def is_path_in_mount(path: Path, mount_point: Path) -> bool:
    candidate = path.as_posix().rstrip("/")
    mount = mount_point.as_posix().rstrip("/")
    return candidate == mount or candidate.startswith(f"{mount}/")


def is_path_covered_by_linux_mount(path: Path, mountinfo_path: Path = Path("/proc/self/mountinfo")) -> bool:
    if not mountinfo_path.exists():
        return False

    for line in mountinfo_path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue

        mount_point = decode_mount_path(fields[4])
        if mount_point == Path("/"):
            continue
        if is_path_in_mount(path, mount_point):
            return True

    return False


def ensure_required_data_mount(db_path: Path) -> None:
    if not is_truthy(os.environ.get("REQUIRE_DATA_MOUNT")):
        return

    if is_path_covered_by_linux_mount(db_path):
        return

    raise RuntimeError(
        "Persistent data mount is required but was not detected for "
        f"{db_path}. Mount a persistent volume or bind mount at /data, "
        "or set REQUIRE_DATA_MOUNT=0 only for disposable/local runs."
    )


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def bot_token_from_path(path: str) -> str | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "webhook":
        return parts[1]
    return None


def parse_json_body(raw_body: bytes) -> tuple[str, str | None, str | None]:
    body_text = raw_body.decode("utf-8", errors="replace")
    if not body_text:
        return body_text, None, "empty request body"

    try:
        parsed = json.loads(body_text)
    except json.JSONDecodeError as exc:
        return body_text, None, f"invalid JSON: {exc.msg}"

    return body_text, json.dumps(parsed, ensure_ascii=False, sort_keys=True), None


def insert_event(
    db_path: Path,
    *,
    bot_token: str | None,
    remote_addr: str,
    method: str,
    path: str,
    headers: dict[str, str],
    raw_body: str,
    payload_json: str | None,
    parse_error: str | None,
) -> int:
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO webhook_events (
                received_at,
                bot_token,
                remote_addr,
                method,
                path,
                headers_json,
                payload_json,
                raw_body,
                parse_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                bot_token,
                remote_addr,
                method,
                path,
                json.dumps(headers, ensure_ascii=False, sort_keys=True),
                payload_json,
                raw_body,
                parse_error,
            ),
        )
        return int(cursor.lastrowid)


def fetch_events(db_path: Path, limit: int, bot_token: str | None) -> list[sqlite3.Row]:
    limit = max(1, min(limit, 500))
    with connect(db_path) as conn:
        if bot_token:
            return list(
                conn.execute(
                    """
                    SELECT * FROM webhook_events
                    WHERE bot_token = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (bot_token, limit),
                )
            )
        return list(
            conn.execute(
                "SELECT * FROM webhook_events ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        )


def fetch_event(db_path: Path, event_id: int) -> sqlite3.Row | None:
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM webhook_events WHERE id = ?",
            (event_id,),
        ).fetchone()


def pretty_json(value: str | None) -> str:
    if not value:
        return ""
    try:
        return json.dumps(json.loads(value), ensure_ascii=False, indent=2, sort_keys=True)
    except json.JSONDecodeError:
        return value


def page_template(title: str, body: str) -> bytes:
    document = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #17202a;
      --muted: #5e6a75;
      --line: #d7dde3;
      --accent: #1877c9;
      --bad: #b3261e;
      --code: #101820;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #111418;
        --panel: #191f25;
        --text: #eef2f5;
        --muted: #a4adb6;
        --line: #313942;
        --accent: #64b5f6;
        --bad: #ffb4ab;
        --code: #0c1116;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header, main {{ max-width: 1180px; margin: 0 auto; padding: 20px; }}
    header {{ display: flex; gap: 12px; align-items: flex-end; justify-content: space-between; }}
    h1 {{ font-size: 24px; margin: 0 0 4px; }}
    p {{ margin: 0; color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    form {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
    input, button {{
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
    }}
    button {{ cursor: pointer; background: var(--accent); color: white; border-color: var(--accent); }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }}
    th {{ text-align: left; color: var(--muted); font-weight: 600; background: color-mix(in srgb, var(--panel), var(--bg) 35%); }}
    tr:last-child td {{ border-bottom: 0; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace; }}
    .muted {{ color: var(--muted); }}
    .error {{ color: var(--bad); }}
    .clip {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    pre {{
      margin: 0;
      padding: 14px;
      overflow: auto;
      background: var(--code);
      color: #f5f7fa;
      border-radius: 8px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .stack {{ display: grid; gap: 16px; }}
    .meta {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }}
    .meta div {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px; }}
    .meta b {{ display: block; margin-bottom: 4px; color: var(--muted); font-size: 12px; text-transform: uppercase; }}
    @media (max-width: 720px) {{
      header {{ display: block; }}
      table, thead, tbody, th, td, tr {{ display: block; }}
      thead {{ display: none; }}
      td {{ border-bottom: 0; padding: 8px 12px; }}
      tr {{ border-bottom: 1px solid var(--line); padding: 8px 0; }}
    }}
  </style>
</head>
<body>
  {body}
</body>
</html>"""
    return document.encode("utf-8")


def render_index(db_path: Path, query: dict[str, list[str]], viewer_base_path: str) -> bytes:
    limit = int(query.get("limit", ["100"])[0] or "100")
    bot_token = (query.get("bot", [""])[0] or "").strip() or None
    events = fetch_events(db_path, limit, bot_token)
    rows = []

    for event in events:
        parse_state = (
            f'<span class="error">{html.escape(event["parse_error"])}</span>'
            if event["parse_error"]
            else '<span class="muted">ok</span>'
        )
        preview = event["payload_json"] or event["raw_body"]
        rows.append(
            "<tr>"
            f'<td class="mono"><a href="{viewer_base_path}/events/{event["id"]}">{event["id"]}</a></td>'
            f'<td class="mono">{html.escape(event["received_at"])}</td>'
            f'<td class="clip mono">{html.escape(event["bot_token"] or "-")}</td>'
            f'<td class="clip mono">{html.escape(event["remote_addr"] or "-")}</td>'
            f"<td>{parse_state}</td>"
            f'<td class="clip mono">{html.escape(preview[:260])}</td>'
            "</tr>"
        )

    rows_html = "\n".join(rows) or (
        '<tr><td colspan="6" class="muted">Пока нет сохраненных вебхуков.</td></tr>'
    )
    bot_value = html.escape(bot_token or "")
    limit_value = html.escape(str(limit))
    body = f"""
<header>
  <div>
    <h1>Telegram Webhook Inbox</h1>
    <p>POST <span class="mono">/webhook/&lt;bot-token&gt;</span> сохраняет входящие JSON-обновления в SQLite.</p>
  </div>
  <form method="get" action="{viewer_base_path}/">
    <input name="bot" value="{bot_value}" placeholder="bot token">
    <input name="limit" value="{limit_value}" inputmode="numeric" size="5" aria-label="limit">
    <button type="submit">Показать</button>
  </form>
</header>
<main>
  <div class="panel">
    <table>
      <thead>
        <tr>
          <th style="width: 70px;">ID</th>
          <th style="width: 190px;">Received</th>
          <th style="width: 190px;">Bot</th>
          <th style="width: 140px;">Remote</th>
          <th style="width: 150px;">JSON</th>
          <th>Payload preview</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</main>
"""
    return page_template("Telegram Webhook Inbox", body)


def render_event(db_path: Path, event_id: int, viewer_base_path: str) -> bytes:
    event = fetch_event(db_path, event_id)
    if event is None:
        return page_template(
            "Event not found",
            f'<header><div><h1>Событие не найдено</h1><p><a href="{viewer_base_path}/">Назад к списку</a></p></div></header>',
        )

    fields: list[tuple[str, Any]] = [
        ("ID", event["id"]),
        ("Received", event["received_at"]),
        ("Bot token", event["bot_token"] or "-"),
        ("Remote", event["remote_addr"] or "-"),
        ("Method", event["method"]),
        ("Path", event["path"]),
        ("Parse error", event["parse_error"] or "none"),
    ]
    meta = "\n".join(
        f"<div><b>{html.escape(label)}</b><span class=\"mono\">{html.escape(str(value))}</span></div>"
        for label, value in fields
    )
    payload = html.escape(pretty_json(event["payload_json"]) or event["raw_body"])
    headers = html.escape(pretty_json(event["headers_json"]))
    body = f"""
<header>
  <div>
    <h1>Webhook event #{event["id"]}</h1>
    <p><a href="{viewer_base_path}/">Назад к списку</a></p>
  </div>
</header>
<main class="stack">
  <section class="meta">{meta}</section>
  <section class="stack">
    <h2>Payload</h2>
    <pre>{payload}</pre>
  </section>
  <section class="stack">
    <h2>Headers</h2>
    <pre>{headers}</pre>
  </section>
</main>
"""
    return page_template(f"Webhook event #{event['id']}", body)


class AppHandler(BaseHTTPRequestHandler):
    db_path: Path = DEFAULT_DB_PATH
    viewer_hash: str = DEFAULT_VIEWER_HASH

    @property
    def viewer_base_path(self) -> str:
        return f"/view/{self.viewer_hash}"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        viewer_base_path = self.viewer_base_path

        if parsed.path in (viewer_base_path, f"{viewer_base_path}/"):
            self.respond_html(render_index(self.db_path, parse_qs(parsed.query), viewer_base_path))
            return

        parts = [part for part in parsed.path.split("/") if part]
        if (
            len(parts) == 4
            and parts[0] == "view"
            and parts[1] == self.viewer_hash
            and parts[2] == "events"
            and parts[3].isdigit()
        ):
            self.respond_html(render_event(self.db_path, int(parts[3]), viewer_base_path))
            return

        self.respond_text("Not found", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        bot_token = bot_token_from_path(parsed.path)
        if not bot_token:
            self.respond_json(
                {"ok": False, "error": "use POST /webhook/<bot-token>"},
                HTTPStatus.NOT_FOUND,
            )
            return

        content_length = int(self.headers.get("content-length", "0") or "0")
        if content_length > MAX_BODY_BYTES:
            self.respond_json(
                {"ok": False, "error": "request body is too large"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return

        raw_body = self.rfile.read(content_length)
        raw_text, payload_json, parse_error = parse_json_body(raw_body)
        event_id = insert_event(
            self.db_path,
            bot_token=bot_token,
            remote_addr=self.client_address[0],
            method=self.command,
            path=self.path,
            headers={key: value for key, value in self.headers.items()},
            raw_body=raw_text,
            payload_json=payload_json,
            parse_error=parse_error,
        )
        self.respond_json({"ok": True, "event_id": event_id})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def respond_html(self, body: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_text(self, text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    parser = argparse.ArgumentParser(description="Store Telegram bot webhooks in local SQLite.")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--viewer-hash", default=os.environ.get("VIEWER_HASH", DEFAULT_VIEWER_HASH))
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.environ.get("DB_PATH", DEFAULT_DB_PATH)),
        help="Path to SQLite database file.",
    )
    args = parser.parse_args()

    db_path = args.db.resolve()
    ensure_required_data_mount(db_path)
    init_db(db_path)
    AppHandler.db_path = db_path
    AppHandler.viewer_hash = args.viewer_hash.strip("/")

    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"Listening on http://{args.host}:{args.port}")
    print(f"SQLite database: {db_path}")
    print(f"Webhook URL path: /webhook/<bot-token>")
    print(f"Viewer URL path: /view/{AppHandler.viewer_hash}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
