"""Strict Board Agent Chat transport; it never invokes a model or sends Slack messages.

Contract reference: ControlMesh 20558b76, native-conversation-cli.py, Paperclip
2026.916.1. Only explicitly paired local Board owners may use this adapter.
Unknown mutation outcomes must be reconciled with GETs, never blindly retried.
"""

import http.client
import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

MAX_RESPONSE = 2 * 1024 * 1024


class NativeChatError(Exception):
    """A bounded, non-secret error. ``uncertain`` forbids automatic resubmission."""

    def __init__(self, code: str, *, uncertain: bool = True):
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise NativeChatError("api_redirect_rejected")


def _identity(value):
    if (not isinstance(value, str) or not value or len(value) > 256
            or any(ord(c) < 32 for c in value)):
        raise NativeChatError("invalid_identity", uncertain=False)
    return value


def _segment(value):
    return urllib.parse.quote(_identity(value), safe="")


def _generation(value):
    return type(value) is int and value >= 0


def _object(value):
    if not isinstance(value, dict):
        raise NativeChatError("api_invalid_schema")
    return value


def _rows(value):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise NativeChatError("api_invalid_schema")
    return value


def _parse_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("invalid constant")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)


class NativeChatApi:
    def __init__(self, base_url, company_id, agent_id, board_user_id="local-board"):
        try:
            parsed = urllib.parse.urlsplit(base_url)
            host = ipaddress.ip_address(parsed.hostname or "")
            valid = (parsed.scheme == "http" and host.is_loopback
                     and parsed.port is not None and 0 < parsed.port <= 65535
                     and not parsed.username and not parsed.password
                     and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
        except (ValueError, TypeError):
            valid = False
        if not valid or board_user_id != "local-board":
            raise NativeChatError("local_board_configuration_required", uncertain=False)
        self.base_url = base_url.rstrip("/")
        self.company_id = _identity(company_id)
        self.agent_id = _identity(agent_id)
        self.board_user_id = board_user_id
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())

    @staticmethod
    def _response_id(value):
        try:
            return _identity(value)
        except NativeChatError:
            raise NativeChatError("api_invalid_identity") from None

    def _request(self, method, path, body=None):
        raw = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=raw, method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=10) as response:
                if response.status != 200 and response.status != 201:
                    raise NativeChatError("api_unexpected_status")
                data = response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as error:
            raise NativeChatError(
                "api_http_" + str(error.code),
                uncertain=error.code >= 500 or error.code == 408) from None
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            raise NativeChatError("api_request_unconfirmed") from None
        if len(data) > MAX_RESPONSE:
            raise NativeChatError("api_response_too_large")
        try:
            return _parse_json(data)
        except (ValueError, UnicodeError):
            raise NativeChatError("api_invalid_response") from None

    def _verify_board(self):
        health = _object(self._request("GET", "/api/health"))
        if health.get("deploymentMode") != "local_trusted":
            raise NativeChatError("local_trusted_runtime_required", uncertain=False)
        session = _object(self._request("GET", "/api/auth/get-session"))
        if (_object(session.get("user")).get("id") != self.board_user_id
                or _object(session.get("session")).get("userId") != self.board_user_id):
            raise NativeChatError("board_identity_mismatch", uncertain=False)
        settings = _object(self._request("GET", "/api/instance/settings/experimental"))
        if settings.get("enableAgentChat") is not True:
            raise NativeChatError("agent_chat_disabled", uncertain=False)

    def _conversation(self, method):
        issue = self._request(method, "/api/companies/" + _segment(self.company_id)
                              + "/chats/" + _segment(self.agent_id))
        if issue is None and method == "GET":
            return None
        issue = _object(issue)
        if (issue.get("companyId") != self.company_id
                or issue.get("conversationAgentId") != self.agent_id
                or issue.get("assigneeAgentId") != self.agent_id
                or issue.get("conversationUserId") != self.board_user_id
                or not _generation(issue.get("conversationSessionGeneration"))):
            raise NativeChatError("conversation_identity_mismatch")
        self._response_id(issue.get("id"))
        return issue

    def _existing(self, issue_id):
        _identity(issue_id)
        issue = self._conversation("GET")
        if issue is None or issue["id"] != issue_id:
            raise NativeChatError("conversation_receipt_mismatch")
        return issue

    def ensure_conversation(self):
        self._verify_board()
        return self._conversation("POST")

    def lookup_conversation(self):
        self._verify_board()
        return self._conversation("GET")

    def _source(self, value, issue_id, text=None, request_id=None):
        source = _object(value)
        if (source.get("issueId") != issue_id or source.get("companyId") != self.company_id
                or source.get("authorUserId") != self.board_user_id
                or source.get("authorAgentId") is not None or source.get("deletedAt")
                or not isinstance(source.get("body"), str)
                or not _generation(source.get("conversationSessionGeneration"))):
            raise NativeChatError("source_comment_identity_mismatch")
        self._response_id(source.get("id"))
        self._response_id(source.get("clientRequestId"))
        if request_id is not None and source["clientRequestId"] != request_id:
            raise NativeChatError("source_request_mismatch")
        if text is not None and source["body"] != text:
            raise NativeChatError("source_comment_text_mismatch")
        return source

    @staticmethod
    def _validate_text(text):
        if (not isinstance(text, str) or not text.strip()
                or len(text.encode("utf-8")) > 32768):
            raise NativeChatError("invalid_message_text", uncertain=False)

    def submit(self, issue_id, text, client_request_id):
        self._validate_text(text)
        _identity(client_request_id)
        self._verify_board()
        self._existing(issue_id)
        value = self._request("POST", "/api/issues/" + _segment(issue_id) + "/comments",
                              {"body": text, "clientRequestId": client_request_id})
        return self._source(value, issue_id, text, client_request_id)

    def reconcile(self, issue_id, text, client_request_id, comment_id=None):
        """Read only. None means not found within an unsaturated recent window."""
        self._validate_text(text)
        _identity(client_request_id)
        self._verify_board()
        self._existing(issue_id)
        path = "/api/issues/" + _segment(issue_id) + "/comments"
        if comment_id is not None:
            source = self._source(self._request("GET", path + "/" + _segment(comment_id)),
                                  issue_id, text, client_request_id)
            if source["id"] != comment_id:
                raise NativeChatError("source_comment_identity_mismatch")
            return source
        rows = _rows(self._request("GET", path + "?order=desc&limit=200"))
        matches = [row for row in rows if row.get("clientRequestId") == client_request_id]
        if len(matches) > 1:
            raise NativeChatError("source_comment_not_unique")
        if matches:
            return self._source(matches[0], issue_id, text, client_request_id)
        if len(rows) >= 200:
            raise NativeChatError("source_lookup_window_exceeded")
        return None

    def status(self, issue_id, source_id, generation):
        """Read only; only explicit successful-run presentation decisions yield final."""
        if not _generation(generation):
            raise NativeChatError("invalid_generation", uncertain=False)
        self._verify_board()
        issue = self._existing(issue_id)
        path = "/api/issues/" + _segment(issue_id)
        source = self._source(self._request("GET", path + "/comments/" + _segment(source_id)),
                              issue_id)
        if source["id"] != source_id:
            raise NativeChatError("source_comment_identity_mismatch")
        base = {"generation": issue["conversationSessionGeneration"]}
        if (source["conversationSessionGeneration"] != generation
                or generation != issue["conversationSessionGeneration"]):
            return {**base, "state": "unknown", "code": "conversation_session_changed"}
        if source["body"].strip() == "/new":
            return {**base, "state": "reset", "text": "已开启新对话，历史记录保留。"}
        runs = _rows(self._request("GET", "/api/companies/" + _segment(self.company_id)
                                  + "/heartbeat-runs?agentId=" + _segment(self.agent_id)
                                  + "&limit=200&summary=true"))
        issue_runs = [run for run in runs
                      if _object(run.get("contextSnapshot", {})).get("issueId") == issue_id]
        # Reference API lists newest first, but never let a shuffled response
        # allow an older successful attempt to mask a newer retry.
        timestamps = []
        for candidate in issue_runs:
            try:
                stamp = candidate.get("createdAt")
                if not isinstance(stamp, str):
                    raise ValueError("missing timestamp")
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("timestamp requires timezone")
                timestamps.append(parsed)
            except ValueError:
                return {**base, "state": "unknown", "code": "run_order_unverified"}
        if len(set(timestamps)) != len(timestamps):
            return {**base, "state": "unknown", "code": "run_order_ambiguous"}
        issue_runs = [row for _, row in sorted(
            zip(timestamps, issue_runs, strict=True), key=lambda pair: pair[0], reverse=True)]
        for candidate in issue_runs[:20]:
            run = _object(self._request("GET", "/api/heartbeat-runs/"
                                        + _segment(candidate.get("id"))))
            if run.get("id") != candidate["id"]:
                raise NativeChatError("run_identity_mismatch")
            context = _object(run.get("contextSnapshot"))
            coalesced = context.get("wakeCommentIds", [])
            if not isinstance(coalesced, list) or any(not isinstance(i, str) for i in coalesced):
                raise NativeChatError("api_invalid_schema")
            ids = [context.get(key) for key in ("commentId", "wakeCommentId", "sourceCommentId")]
            if (run.get("companyId") != self.company_id or run.get("agentId") != self.agent_id
                    or context.get("issueId") != issue_id or source_id not in ids + coalesced):
                continue
            if (not _generation(context.get("conversationSessionGeneration"))
                    or context["conversationSessionGeneration"] != generation):
                return {**base, "state": "unknown", "code": "conversation_session_changed"}
            result = {**base, "runId": run["id"]}
            state = run.get("status")
            if state in {"queued", "running"}:
                return {**result, "state": "pending"}
            if state in {"failed", "cancelled", "interrupted", "timed_out"}:
                return {**result, "state": "failed", "code": "run_" + state}
            if state != "succeeded":
                return {**result, "state": "unknown", "code": "unrecognized_run_status"}
            data = _object(run.get("resultJson"))
            decision = data.get("presentationDecision")
            if (not isinstance(decision, dict)
                    or decision.get("schema") != "paperclip.run_presentation_decision.v1"
                    or not isinstance(decision.get("commentId"), str)
                    or not decision["commentId"]):
                return {**result, "state": "unknown", "code": "no_final_presentation_decision"}
            # Direct read avoids mistaking a truncated comment window for no reply.
            reply = _object(self._request("GET", path + "/comments/"
                                           + _segment(decision["commentId"])))
            if (reply.get("id") != decision["commentId"] or reply.get("issueId") != issue_id
                    or reply.get("companyId") != self.company_id
                    or reply.get("authorAgentId") != self.agent_id
                    or reply.get("authorUserId") is not None
                    or reply.get("createdByRunId") != run["id"] or reply.get("deletedAt")
                    or not isinstance(reply.get("body"), str) or not reply["body"].strip()):
                raise NativeChatError("reply_identity_mismatch")
            return {**result, "state": "final", "replyCommentId": reply["id"],
                    "text": reply["body"]}
        code = ("reply_lookup_window_exceeded" if len(issue_runs) > 20 or len(runs) >= 200
                else "run_not_found")
        return {**base, "state": "unknown" if code.endswith("exceeded") else "pending",
                "code": code}
