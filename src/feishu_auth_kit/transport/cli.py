"""Host-profile send CLI; no registration and no credential output."""

import argparse
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

from .cloud_bridge import BridgeError, EncryptedStore
from .sender import TransportSender


def factory(reference):
    module, name = reference.split(":", 1)
    return getattr(importlib.import_module(module), name)()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["send", "status"])
    parser.add_argument("--operation", required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--store-factory", help="Host durable store module:factory")
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--provider", help="Existing host token provider module:factory")
    auth.add_argument(
        "--executor", help="Normal authenticated host CLI request backend module:factory"
    )
    parser.add_argument("--text-file", type=Path)
    parser.add_argument("--reply-to")
    parser.add_argument("--reply-in-thread", action="store_true")
    parser.add_argument("--target-chat")
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args(argv)
    if args.action == "send" and (not (args.provider or args.executor) or not args.text_file):
        parser.error("send requires --provider or --executor, and --text-file")
    import requests

    session = requests.Session()
    session.trust_env = False
    sender = None
    try:
        store = (
            factory(args.store_factory) if args.store_factory else EncryptedStore(args.state_dir)
        )
        if args.action == "send" and args.executor:
            sender = TransportSender(
                store, request_executor=factory(args.executor), timeout=args.timeout
            )
        else:
            provider = (
                factory(args.provider)
                if args.action == "send"
                else SimpleNamespace(app_id=store.read("bot")["app_id"])
            )
            sender = TransportSender(
                store, auth_provider=provider, session=session, timeout=args.timeout
            )
        if args.action == "send":
            text = args.text_file.read_text(encoding="utf-8")
            result = sender.send(
                args.operation,
                text,
                reply_to=args.reply_to,
                target_chat=args.target_chat,
                reply_in_thread=args.reply_in_thread,
            )
        else:
            result = sender.status(args.operation)
        print(json.dumps(result))
        return 0
    except Exception as exc:
        # If result persistence failed after POST, return unknown conservatively.
        result = {
            "status": "failed" if isinstance(exc, BridgeError) else "unknown",
            "automatic_resend": False,
            "error_type": type(exc).__name__,
        }
        if sender is not None:
            try:
                result["operation_status"] = sender.status(args.operation)
            except Exception:
                pass
        print(json.dumps(result))
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
