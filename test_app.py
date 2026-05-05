from __future__ import annotations

import http.client
import json
import os
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import app


class WebhookInboxTests(unittest.TestCase):
    def test_data_mount_check_requires_explicit_mount(self) -> None:
        mountinfo_path = app.BASE_DIR / f"test_mountinfo_{os.getpid()}.txt"
        try:
            mountinfo_path.write_text(
                "1 0 8:1 / / rw,relatime - ext4 /dev/root rw\n",
                encoding="utf-8",
            )
            self.assertFalse(
                app.is_path_covered_by_linux_mount(Path("/data/telegram_webhooks.sqlite3"), mountinfo_path)
            )

            mountinfo_path.write_text(
                "1 0 8:1 / / rw,relatime - ext4 /dev/root rw\n"
                "2 1 0:42 / /data rw,relatime - ext4 /dev/volume rw\n",
                encoding="utf-8",
            )
            self.assertTrue(
                app.is_path_covered_by_linux_mount(Path("/data/telegram_webhooks.sqlite3"), mountinfo_path)
            )
        finally:
            if mountinfo_path.exists():
                mountinfo_path.unlink()

    def test_post_webhook_is_saved_to_sqlite(self) -> None:
        db_path = app.BASE_DIR / f"test_unittest_events_{os.getpid()}.sqlite3"
        if db_path.exists():
            db_path.unlink()
        try:
            app.init_db(db_path)
            app.AppHandler.db_path = db_path
            app.AppHandler.viewer_hash = "unit-test-secret"

            server = ThreadingHTTPServer(("127.0.0.1", 0), app.AppHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            try:
                port = server.server_address[1]
                payload = {"update_id": 1, "message": {"text": "hello"}}
                body = json.dumps(payload).encode("utf-8")

                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request(
                    "POST",
                    "/webhook/test-bot",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
                response_body = json.loads(response.read().decode("utf-8"))
                conn.close()

                self.assertEqual(response.status, 200)
                self.assertEqual(response_body["ok"], True)

                events = app.fetch_events(db_path, limit=10, bot_token="test-bot")
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["bot_token"], "test-bot")
                self.assertIsNone(events[0]["parse_error"])
                self.assertEqual(json.loads(events[0]["payload_json"]), payload)

                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/")
                root_response = conn.getresponse()
                root_response.read()
                conn.close()
                self.assertEqual(root_response.status, 404)

                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/view/unit-test-secret/")
                viewer_response = conn.getresponse()
                viewer_body = viewer_response.read().decode("utf-8")
                conn.close()
                self.assertEqual(viewer_response.status, 200)
                self.assertIn("/view/unit-test-secret/events/", viewer_body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        finally:
            if db_path.exists():
                for attempt in range(10):
                    try:
                        db_path.unlink()
                        break
                    except PermissionError:
                        if attempt == 9:
                            break
                        time.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
