"""Durable transport ledger for one explicitly paired Slack owner DM.

This is a transport, not an agent scheduler. Paperclip owns execution and context.
Unknown writes are reconciled by reads; they are never automatically replayed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any


class LedgerSchemaError(ValueError):
    """Existing ledger needs an explicit, evidence-preserving migration."""


class ContinuityStore:
    def __init__(self, path: str, binding: Any):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS binding (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), fingerprint TEXT NOT NULL,
          config TEXT NOT NULL, issue_id TEXT, generation INTEGER NOT NULL DEFAULT 0,
          native_generation INTEGER, reset_ts TEXT, creation TEXT NOT NULL DEFAULT 'not_started');
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE NOT NULL,
          event_id TEXT NOT NULL, payload TEXT NOT NULL, generation INTEGER NOT NULL,
          is_reset INTEGER NOT NULL DEFAULT 0,
          received_at REAL NOT NULL, execution TEXT NOT NULL DEFAULT 'not_started',
          source_id TEXT, native_generation INTEGER, run_id TEXT, reply_id TEXT,
          final_text TEXT, delivery TEXT NOT NULL DEFAULT 'not_ready', slack_ts TEXT,
          error TEXT);
        CREATE TABLE IF NOT EXISTS deliveries (
          reply_key TEXT PRIMARY KEY, request_id TEXT NOT NULL, state TEXT NOT NULL,
          slack_ts TEXT);
        """)
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        required = {
            "binding": {"reset_ts", "generation", "native_generation", "creation"},
            "events": {"is_reset", "request_id", "generation", "delivery"},
            "deliveries": {"reply_key", "request_id", "state", "slack_ts"},
        }
        compatible = all(
            columns <= {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for table, columns in required.items()
        )
        if version not in (0, 1) or not compatible:
            self.db.close()
            raise LedgerSchemaError(
                "Unsupported ledger schema. Preserve the database; "
                "an explicit migration is required. Never delete it to retry."
            )
        self.db.execute("PRAGMA user_version=1")
        config = json.dumps(asdict(binding), sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(config.encode()).hexdigest()
        self.db.execute(
            "INSERT OR IGNORE INTO binding(singleton,fingerprint,config) VALUES(1,?,?)",
            (fingerprint, config),
        )
        if self.meta()["fingerprint"] != fingerprint:
            raise ValueError("ledger is bound to a different Slack/native identity")

    def close(self):
        self.db.close()

    def meta(self):
        return dict(self.db.execute("SELECT * FROM binding").fetchone())

    def events(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM events ORDER BY seq")]

    def receive(self, event: dict) -> dict:
        """Commit receipt before HTTP ACK. Duplicate source identity cannot reset twice."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute(
                "SELECT * FROM events WHERE request_id=?", (event["client_request_id"],)
            ).fetchone()
            if prior:
                original = json.loads(prior["payload"])
                # Envelope event_id may differ on provider retries; content must not.
                for key in (
                    "text",
                    "workspace_id",
                    "app_id",
                    "user_id",
                    "channel_id",
                    "message_ts",
                ):
                    if original[key] != event[key]:
                        raise ValueError("source identity reused with different content")
                self.db.execute("COMMIT")
                return dict(prior)
            meta = self.meta()
            generation = meta["generation"]
            timestamp = Decimal(event["message_ts"])
            if not timestamp.is_finite() or timestamp < 0:
                raise ValueError("invalid source timestamp")
            stale = meta["reset_ts"] is not None and timestamp < Decimal(meta["reset_ts"])
            if event["text"].strip() == "/new" and not stale:
                generation += 1
                self.db.execute(
                    "UPDATE binding SET generation=?,reset_ts=?", (generation, event["message_ts"])
                )
            self.db.execute(
                """INSERT INTO events(request_id,event_id,payload,generation,received_at,is_reset)
                             VALUES(?,?,?,?,?,?)""",
                (
                    event["client_request_id"],
                    event["event_id"],
                    json.dumps(event),
                    generation,
                    time.time(),
                    int(event["text"].strip() == "/new"),
                ),
            )
            if stale:
                self.update(
                    event["client_request_id"],
                    execution="superseded",
                    delivery="suppressed",
                    error="before_reset_source_timestamp",
                )
            row = self.db.execute(
                "SELECT * FROM events WHERE request_id=?", (event["client_request_id"],)
            ).fetchone()
            self.db.execute("COMMIT")
            return dict(row)
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def update(self, request_id: str, **values):
        allowed = {
            "execution",
            "source_id",
            "native_generation",
            "run_id",
            "reply_id",
            "final_text",
            "delivery",
            "slack_ts",
            "error",
        }
        if not values or not set(values) <= allowed:
            raise ValueError("invalid ledger fields")
        self.db.execute(
            "UPDATE events SET " + ",".join(f"{k}=?" for k in values) + " WHERE request_id=?",
            (*values.values(), request_id),
        )


class SlackContinuityBridge:
    def __init__(self, store: ContinuityStore, native: Any, sender: Any):
        self.store, self.native, self.sender = store, native, sender

    def _conversation(self):
        db = self.store.db
        meta = self.store.meta()
        if meta["issue_id"]:
            return meta["issue_id"]
        # Atomically claim the only permitted create attempt. A restart does GET only.
        claimed = db.execute("UPDATE binding SET creation='unknown' WHERE creation='not_started'")
        try:
            issue = (
                self.native.ensure_conversation()
                if claimed.rowcount
                else self.native.lookup_conversation()
            )
            if (not issue or type(issue.get("conversationSessionGeneration")) is not int
                    or issue["conversationSessionGeneration"] < 0):
                return None
            db.execute(
                "UPDATE binding SET issue_id=?,native_generation=?,creation='confirmed'",
                (issue["id"], issue["conversationSessionGeneration"]),
            )
            return issue["id"]
        except Exception:
            return None

    def tick(self):
        """One bounded transport pass. No agent spawning, retries, or paid API calls."""
        issue_id = self._conversation()
        if not issue_id:
            return
        for row in self.store.events():
            event = json.loads(row["payload"])
            request_id = row["request_id"]
            latest_generation = self.store.meta()["generation"]
            is_reset = event["text"].strip() == "/new"
            # Receipt of /new fences unclaimed late results, even before native reset.
            if row["generation"] < latest_generation:
                if row["delivery"] == "unknown":
                    self._reconcile_delivery(issue_id, row, event)
                if row["delivery"] not in ("delivered", "unknown", "sending"):
                    self.store.update(request_id, delivery="suppressed")
                if row["execution"] == "not_started":
                    self.store.update(request_id, execution="superseded")
                continue
            if row["execution"] == "not_started":
                # A reset command ahead in this generation must be confirmed first.
                barrier = self.store.db.execute(
                    """SELECT 1 FROM events WHERE seq<?
                    AND generation=? AND is_reset=1
                    AND execution NOT IN ('reset','superseded') LIMIT 1""",
                    (row["seq"], row["generation"]),
                ).fetchone()
                if barrier:
                    continue
                claimed = self.store.db.execute(
                    "UPDATE events SET execution='unknown' "
                    "WHERE request_id=? AND execution='not_started'",
                    (request_id,),
                )
                if not claimed.rowcount:
                    continue
                try:
                    source = self.native.submit(issue_id, event["text"], request_id)
                except Exception:
                    continue
                self._source(row, source)
            elif row["execution"] == "unknown":
                try:
                    source = self.native.reconcile(
                        issue_id, event["text"], request_id, row["source_id"]
                    )
                except Exception:
                    continue
                if not source:
                    continue
                self._source(row, source)
            row = dict(
                self.store.db.execute(
                    "SELECT * FROM events WHERE request_id=?", (request_id,)
                ).fetchone()
            )
            if row["execution"] == "pending":
                try:
                    result = self.native.status(
                        issue_id, row["source_id"], row["native_generation"]
                    )
                except Exception:
                    continue
                state = result.get("state")
                if state == "reset" and is_reset:
                    self.store.update(request_id, execution="reset", delivery="not_ready")
                    self.store.db.execute(
                        "UPDATE binding SET native_generation=?", (row["native_generation"],)
                    )
                elif state == "failed":
                    self.store.update(request_id, execution="failed")
                elif state == "final" and not is_reset:
                    if not all(result.get(k) for k in ("runId", "replyCommentId", "text")):
                        continue
                    self.store.update(
                        request_id,
                        execution="final",
                        run_id=result["runId"],
                        reply_id=result["replyCommentId"],
                        final_text=result["text"],
                        delivery="ready",
                    )
            row = dict(
                self.store.db.execute(
                    "SELECT * FROM events WHERE request_id=?", (request_id,)
                ).fetchone()
            )
            if row["delivery"] == "ready":
                self._deliver(issue_id, row, event)
            elif row["delivery"] == "unknown":
                self._reconcile_delivery(issue_id, row, event)

    def _source(self, row, source):
        generation = source.get("conversationSessionGeneration")
        evidence = source.get("generationEvidence")
        if generation is None and isinstance(evidence, dict):
            if (evidence.get("kind") != "source_run_v1"
                    or evidence.get("sourceId") != source.get("id")
                    or evidence.get("clientRequestId") != row["request_id"]
                    or evidence.get("issueId") != self.store.meta()["issue_id"]
                    or not evidence.get("runId") or not evidence.get("sessionId")
                    or row["is_reset"]):
                return
            generation = evidence.get("generation")
        if not source.get("id") or type(generation) is not int or generation < 0:
            return
        self.store.update(
            row["request_id"],
            execution="pending",
            source_id=source["id"],
            native_generation=generation,
            **({"run_id": evidence["runId"]} if isinstance(evidence, dict) else {}),
        )

    def _deliver(self, issue_id, row, event):
        db = self.store.db
        request_id = row["request_id"]
        key = issue_id + ":" + row["reply_id"]
        db.execute("BEGIN IMMEDIATE")
        try:
            if row["generation"] != self.store.meta()["generation"]:
                self.store.update(request_id, delivery="suppressed")
                db.execute("COMMIT")
                return
            # Coalesced sources share one native final comment. Claim before external I/O.
            claim = db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES(?,?,?,NULL)", (key, request_id, "unknown")
            )
            if not claim.rowcount:
                receipt = db.execute(
                    "SELECT * FROM deliveries WHERE reply_key=?", (key,)
                ).fetchone()
                self.store.update(
                    request_id, delivery=receipt["state"], slack_ts=receipt["slack_ts"]
                )
                db.execute("COMMIT")
                return
            self.store.update(request_id, delivery="unknown")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        try:
            receipt = self.sender.send(event["channel_id"], row["final_text"], request_id)
            ts = receipt.get("ts")
            if (
                receipt.get("ok") is not True
                or receipt.get("channel") != event["channel_id"]
                or not ts
            ):
                return
        except Exception:
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "UPDATE deliveries SET state='delivered',slack_ts=? WHERE reply_key=?", (ts, key)
            )
            self.store.update(request_id, delivery="delivered", slack_ts=ts)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def _reconcile_delivery(self, issue_id, row, event):
        # Coalesced source rows share the first send's UUID, never a new UUID.
        key = issue_id + ":" + row["reply_id"]
        delivery = self.store.db.execute(
            "SELECT * FROM deliveries WHERE reply_key=?", (key,)
        ).fetchone()
        if not delivery:
            return
        original = self.store.db.execute(
            "SELECT payload FROM events WHERE request_id=?", (delivery["request_id"],)
        ).fetchone()
        if not original:
            return
        source_event = json.loads(original["payload"])
        # Optional existing history permission only. Missing scopes/errors stay unknown.
        try:
            receipt = self.sender.reconcile(
                event["channel_id"],
                row["final_text"],
                delivery["request_id"],
                source_event["message_ts"],
            )
            if (
                not receipt
                or receipt.get("ok") is not True
                or receipt.get("channel") != event["channel_id"]
                or not receipt.get("ts")
            ):
                return
        except Exception:
            return
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            self.store.db.execute(
                "UPDATE deliveries SET state='delivered',slack_ts=? WHERE reply_key=?",
                (receipt["ts"], key),
            )
            self.store.db.execute(
                "UPDATE events SET delivery='delivered',slack_ts=? "
                "WHERE reply_id=? AND delivery='unknown'",
                (receipt["ts"], row["reply_id"]),
            )
            self.store.db.execute("COMMIT")
        except BaseException:
            self.store.db.execute("ROLLBACK")
            raise
