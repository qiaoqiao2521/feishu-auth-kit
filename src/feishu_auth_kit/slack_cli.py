"""Small standalone Slack transport CLI. No credentials are written to the ledger."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .slack_continuity import ContinuityStore, LedgerSchemaError, SlackContinuityBridge
from .slack_native_api import NativeChatApi
from .slack_transport import SlackBinding, SlackSender, parse_slack_request


def load_binding(path):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or set(data) - {field.name for field in fields(SlackBinding)}:
        raise ValueError("binding contains unsupported fields")
    return SlackBinding(**data)


def serve(store, binding, signing_secret, host, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log source content, signatures, tokens or URL query strings.

        def do_POST(self):
            self.connection.settimeout(5)
            if self.path != "/slack/events":
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1_048_576 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid request size")
                raw = self.rfile.read(size)
                event = parse_slack_request(raw, self.headers, signing_secret, binding)
                if "challenge" in event:
                    response = {"challenge": event["challenge"]}
                else:
                    receipt = store.receive(event)
                    response = {"ok": True, "receipt": receipt["request_id"]}
            except (ValueError, KeyError, TypeError):
                self.send_error(403, "Rejected Slack request")
                return
            except Exception:
                self.send_error(503, "Receipt persistence failed")
                return
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer((host, port), Handler)
    server.timeout = 1
    server.serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, help="explicit owner DM/native identity JSON")
    parser.add_argument("--state", required=True, help="durable SQLite ledger path")
    sub = parser.add_subparsers(dest="command", required=True)
    ingress = sub.add_parser("serve", help="verify and persist Slack Events API requests only")
    ingress.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1"])
    ingress.add_argument("--port", default=8788, type=int)
    worker = sub.add_parser("pump", help="bounded transport pass, or explicit foreground loop")
    worker.add_argument("--loop", action="store_true")
    worker.add_argument("--interval", type=float, default=2)
    sub.add_parser("status", help="read persisted phase receipts, without source message bodies")
    args = parser.parse_args(argv)
    binding = load_binding(args.binding)
    # Local files contain user message text. New files are owner-readable only.
    os.umask(0o077)
    store = ContinuityStore(args.state, binding)
    try:
        if args.command == "status":
            records = [
                {
                    key: row[key]
                    for key in (
                        "request_id",
                        "generation",
                        "received_at",
                        "execution",
                        "source_id",
                        "run_id",
                        "reply_id",
                        "delivery",
                        "slack_ts",
                    )
                }
                for row in store.events()
            ]
            print(
                json.dumps(
                    {
                        "conversationIssueId": store.meta()["issue_id"],
                        "generation": store.meta()["generation"],
                        "receipts": records,
                    }
                )
            )
        elif args.command == "serve":
            secret = os.environ.get("SLACK_SIGNING_SECRET")
            if not secret:
                raise ValueError("SLACK_SIGNING_SECRET is required")
            serve(store, binding, secret, args.host, args.port)
        else:
            if args.interval < 0.5:
                raise ValueError("interval must be at least 0.5 seconds")
            token = os.environ.get("SLACK_BOT_TOKEN")
            if not token:
                raise ValueError("SLACK_BOT_TOKEN is required")
            native = NativeChatApi(
                binding.server_url, binding.company_id, binding.agent_id, binding.board_user_id
            )
            bridge = SlackContinuityBridge(store, native, SlackSender(token))
            # One foreground pump per ledger. Lock released automatically on process death.
            import fcntl

            with open(args.state + ".pump.lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                while True:
                    bridge.tick()
                    if not args.loop:
                        break
                    time.sleep(args.interval)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LedgerSchemaError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except (ValueError, OSError):
        print(
            "Slack bridge stopped: invalid configuration or unavailable local resource",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
