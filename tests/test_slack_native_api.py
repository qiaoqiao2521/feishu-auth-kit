"""Isolated HTTP-boundary tests: no server, credentials, model, or Slack I/O."""
import copy
import io
import json
import unittest
import urllib.error
from unittest.mock import Mock

from feishu_auth_kit.slack_native_api import MAX_RESPONSE, NativeChatApi, NativeChatError

ISSUE = {
    "id": "issue", "companyId": "company", "conversationAgentId": "agent",
    "assigneeAgentId": "agent", "conversationUserId": "local-board",
    "conversationSessionGeneration": 0,
}
SOURCE = {
    "id": "source", "issueId": "issue", "companyId": "company",
    "authorUserId": "local-board", "authorAgentId": None,
    "clientRequestId": "request", "body": "hello", "conversationSessionGeneration": 0,
}
RUN = {
    "id": "run", "companyId": "company", "agentId": "agent", "status": "succeeded",
    "createdAt": "2026-10-10T05:00:00.000Z",
    "contextSnapshot": {"issueId": "issue", "wakeCommentIds": ["source"],
                        "conversationSessionGeneration": 0},
    "resultJson": {"presentationDecision": {
        "schema": "paperclip.run_presentation_decision.v1", "commentId": "reply"}},
}
REPLY = {
    "id": "reply", "companyId": "company", "issueId": "issue",
    "authorAgentId": "agent", "authorUserId": None, "createdByRunId": "run",
    "body": "final reply",
}


class Response(io.BytesIO):
    status = 200


class NativeChatApiTests(unittest.TestCase):
    def setUp(self):
        self.api = NativeChatApi("http://127.0.0.1:3100", "company", "agent")
        self.calls = []
        self.responses = []
        self.api._opener.open = Mock(side_effect=self.open)

    def open(self, request, timeout):
        self.assertEqual(timeout, 10)
        self.calls.append((request.method, request.full_url,
                           json.loads(request.data) if request.data else None))
        self.assertTrue(self.responses, "Unexpected HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, bytes):
            return Response(response)
        return Response(json.dumps(response).encode())

    def setup_responses(self, *values, issue=ISSUE):
        self.responses = [
            {"deploymentMode": "local_trusted"},
            {"user": {"id": "local-board"}, "session": {"userId": "local-board"}},
            {"enableAgentChat": True}, copy.deepcopy(issue), *copy.deepcopy(values),
        ]

    def assert_read_only(self):
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls))
        self.assertFalse(self.responses)

    def test_reject_non_loopback_credentials_and_other_board_identity(self):
        for url in ("https://127.0.0.1:3100", "http://localhost:3100",
                    "http://example.com:3100", "http://127.0.0.1:3100/path",
                    "http://user@127.0.0.1:3100", "http://127.0.0.1:3100?x=1"):
            with self.subTest(url=url), self.assertRaises(NativeChatError):
                NativeChatApi(url, "company", "agent")
        with self.assertRaises(NativeChatError):
            NativeChatApi("http://127.0.0.1:3100", "company", "agent", "someone")

    def test_ensure_and_submit_exact_http_contract(self):
        self.setup_responses()
        self.assertEqual(self.api.ensure_conversation(), ISSUE)
        self.assertEqual(self.calls[-1][0:2],
                         ("POST", "http://127.0.0.1:3100/api/companies/company/chats/agent"))
        self.setup_responses(SOURCE)
        self.assertEqual(self.api.submit("issue", "hello", "request"), SOURCE)
        self.assertEqual(self.calls[-1], ("POST", "http://127.0.0.1:3100/api/issues/issue/comments",
                                         {"body": "hello", "clientRequestId": "request"}))

    def test_lookup_null_is_read_only(self):
        self.setup_responses(issue=None)
        self.assertIsNone(self.api.lookup_conversation())
        self.assert_read_only()

    def test_reconcile_by_request_or_receipt_never_posts(self):
        for receipt in (None, "source"):
            self.calls.clear()
            self.setup_responses(SOURCE if receipt else [SOURCE])
            self.assertEqual(self.api.reconcile("issue", "hello", "request", receipt), SOURCE)
            self.assert_read_only()

    def test_reconcile_missing_and_saturated_window(self):
        self.setup_responses([])
        self.assertIsNone(self.api.reconcile("issue", "hello", "request"))
        self.setup_responses([{"clientRequestId": "other"}] * 200)
        with self.assertRaisesRegex(NativeChatError, "source_lookup_window_exceeded"):
            self.api.reconcile("issue", "hello", "request")
        self.assert_read_only()

    def test_reconcile_identity_and_text_mismatch_fail_closed(self):
        for patch in ({"authorUserId": "other"}, {"body": "different"},
                      {"companyId": "other"}, {"authorAgentId": "agent"}):
            self.setup_responses([{**SOURCE, **patch}])
            with self.assertRaises(NativeChatError):
                self.api.reconcile("issue", "hello", "request")
        self.assert_read_only()

    def test_final_requires_successful_run_source_and_presentation_identity(self):
        self.setup_responses(SOURCE, [RUN], RUN, REPLY)
        self.assertEqual(self.api.status("issue", "source", 0), {
            "state": "final", "generation": 0, "runId": "run",
            "replyCommentId": "reply", "text": "final reply"})
        self.assert_read_only()

    def test_latest_run_supersedes_old_success(self):
        newer = {**RUN, "id": "new-run", "status": "running",
                 "createdAt": "2026-10-10T05:01:00.000Z"}
        self.setup_responses(SOURCE, [RUN, newer], newer)
        result = self.api.status("issue", "source", 0)
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["runId"], "new-run")
        self.assert_read_only()

    def test_ambiguous_run_order_never_selects_old_success(self):
        self.setup_responses(SOURCE, [RUN, {**RUN, "id": "retry", "status": "running"}])
        self.assertEqual(self.api.status("issue", "source", 0)["code"], "run_order_ambiguous")
        self.setup_responses(SOURCE, [{**RUN, "createdAt": None}])
        self.assertEqual(self.api.status("issue", "source", 0)["code"], "run_order_unverified")
        self.assert_read_only()

    def test_no_final_decision_never_falls_back_to_progress(self):
        for result in ({}, {"presentationDecision": {"schema": "other", "commentId": "reply"}},
                       {"presentationDecision": {
                           "schema": "paperclip.run_presentation_decision.v1", "commentId": None}}):
            self.setup_responses(SOURCE, [RUN], {**RUN, "resultJson": result})
            self.assertEqual(self.api.status("issue", "source", 0)["state"], "unknown")
        self.assert_read_only()

    def test_unrelated_source_run_cannot_supply_final(self):
        wrong = copy.deepcopy(RUN)
        wrong["contextSnapshot"]["wakeCommentIds"] = ["another-source"]
        self.setup_responses(SOURCE, [wrong], wrong)
        self.assertEqual(self.api.status("issue", "source", 0)["state"], "pending")
        self.assert_read_only()

    def test_foreign_reply_fails_closed(self):
        for patch in ({"createdByRunId": "another-run"}, {"id": "other"},
                      {"authorAgentId": "other"}, {"authorUserId": "local-board"}):
            self.setup_responses(SOURCE, [RUN], RUN, {**REPLY, **patch})
            with self.assertRaisesRegex(NativeChatError, "reply_identity_mismatch"):
                self.api.status("issue", "source", 0)
        self.assert_read_only()

    def test_native_new_requires_confirmed_generation_and_preserved_issue(self):
        new_source = {**SOURCE, "body": "/new", "conversationSessionGeneration": 1}
        self.setup_responses(new_source, issue={**ISSUE, "conversationSessionGeneration": 1})
        result = self.api.status("issue", "source", 1)
        self.assertEqual(result["state"], "reset")
        self.assertEqual(result["generation"], 1)
        self.setup_responses(new_source, issue=ISSUE)
        self.assertEqual(self.api.status("issue", "source", 1)["state"], "unknown")
        self.assert_read_only()

    def test_failed_run_never_returns_reply(self):
        self.setup_responses(SOURCE, [RUN], {**RUN, "status": "failed"})
        self.assertEqual(self.api.status("issue", "source", 0)["state"], "failed")
        self.assert_read_only()

    def test_disabled_chat_blocks_post(self):
        self.responses = [{"deploymentMode": "local_trusted"},
                          {"user": {"id": "local-board"}, "session": {"userId": "local-board"}},
                          {"enableAgentChat": False}]
        with self.assertRaisesRegex(NativeChatError, "agent_chat_disabled"):
            self.api.ensure_conversation()
        self.assert_read_only()

    def test_timeout_after_post_is_uncertain_without_retry(self):
        self.setup_responses(TimeoutError())
        with self.assertRaises(NativeChatError) as caught:
            self.api.submit("issue", "hello", "request")
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(sum(method == "POST" for method, _, _ in self.calls), 1)

    def test_http_500_bad_json_and_oversize_are_uncertain(self):
        cases = [urllib.error.HTTPError("http://127.0.0.1:3100", 500, "error", {}, None),
                 b'{"id":"x","id":"y"}', b"x" * (MAX_RESPONSE + 1)]
        for response in cases:
            self.responses = [response]
            with self.assertRaises(NativeChatError) as caught:
                self.api._request("POST", "/api/test")
            self.assertTrue(caught.exception.uncertain)

    def test_malformed_success_receipt_remains_uncertain(self):
        self.setup_responses({**SOURCE, "id": None})
        with self.assertRaises(NativeChatError) as caught:
            self.api.submit("issue", "hello", "request")
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(sum(method == "POST" for method, _, _ in self.calls), 1)

    def test_foreign_board_identity_blocks_all_mutations(self):
        self.responses = [{"deploymentMode": "local_trusted"},
                          {"user": {"id": "other"}, "session": {"userId": "other"}}]
        with self.assertRaisesRegex(NativeChatError, "board_identity_mismatch"):
            self.api.ensure_conversation()
        self.assert_read_only()

    def test_stale_and_malformed_run_generations_fail_closed(self):
        for generation in (True, None, 2):
            run = copy.deepcopy(RUN)
            run["contextSnapshot"]["conversationSessionGeneration"] = generation
            self.setup_responses(SOURCE, [run], run)
            self.assertEqual(self.api.status("issue", "source", 0)["state"], "unknown")
        self.assert_read_only()

    def test_redirect_handler_does_not_follow(self):
        from feishu_auth_kit.slack_native_api import _NoRedirect
        handler = _NoRedirect()
        with self.assertRaisesRegex(NativeChatError, "api_redirect_rejected"):
            handler.redirect_request(None, None, 302, "redirect", {}, "https://example.com")


if __name__ == "__main__":
    unittest.main()


class LegacyGenerationEvidenceTests(unittest.TestCase):
    setUp = NativeChatApiTests.setUp
    open = NativeChatApiTests.open
    setup_responses = NativeChatApiTests.setup_responses
    assert_read_only = NativeChatApiTests.assert_read_only

    def legacy(self):
        return {**SOURCE, "conversationSessionGeneration": None}

    def evidence_run(self, **patch):
        return {**copy.deepcopy(RUN), "sessionIdAfter": "provider-session", **patch}

    def test_reconcile_preserves_null_and_returns_separate_source_run_proof(self):
        source, run = self.legacy(), self.evidence_run()
        self.setup_responses([source], [run], run, ISSUE, source)
        receipt = self.api.reconcile("issue", "hello", "request")
        self.assertIsNone(receipt["conversationSessionGeneration"])
        self.assertEqual(receipt["generationEvidence"], {
            "kind": "source_run_v1", "sourceId": "source", "issueId": "issue",
            "clientRequestId": "request", "runId": "run",
            "sessionId": "provider-session", "generation": 0})
        self.assert_read_only()

    def test_submit_null_has_one_post_and_then_read_only_evidence(self):
        source, run = self.legacy(), self.evidence_run()
        self.setup_responses(source, [run], run, ISSUE, source)
        self.assertIsNone(self.api.submit("issue", "hello", "request")
                          ["conversationSessionGeneration"])
        self.assertEqual(sum(method == "POST" for method, _, _ in self.calls), 1)
        self.assertFalse(self.responses)

    def test_final_rechecks_source_run_session_and_generation(self):
        source, run = self.legacy(), self.evidence_run()
        self.setup_responses(source, [run], run, REPLY, [run], run, ISSUE, source)
        result = self.api.status("issue", "source", 0)
        self.assertEqual(result["state"], "final")
        self.assert_read_only()

    def test_missing_field_is_not_legacy_null(self):
        for value in (True, -1, "0", "missing"):
            source = {**SOURCE, "conversationSessionGeneration": value}
            if value == "missing":
                del source["conversationSessionGeneration"]
            self.setup_responses([source])
            with self.assertRaisesRegex(NativeChatError, "source_comment_identity_mismatch"):
                self.api.reconcile("issue", "hello", "request")
        self.assert_read_only()

    def test_null_reset_waits_for_its_own_boundary_comment(self):
        self.setup_responses([{**self.legacy(), "body": "/new"}])
        with self.assertRaisesRegex(NativeChatError, "reset_generation_unverified"):
            self.api.reconcile("issue", "/new", "request")
        source = {**SOURCE, "body": "/new", "conversationSessionGeneration": 1}
        self.setup_responses([source], issue={**ISSUE, "conversationSessionGeneration": 1})
        self.assertEqual(self.api.reconcile("issue", "/new", "request"), source)
        self.assert_read_only()

    def test_old_run_after_reset_cannot_borrow_current_generation(self):
        source, run = self.legacy(), self.evidence_run()
        new = {**ISSUE, "conversationSessionGeneration": 1}
        self.setup_responses([source], [run], run, new, issue=new)
        with self.assertRaisesRegex(NativeChatError, "conversation_session_changed"):
            self.api.reconcile("issue", "hello", "request")
        self.assert_read_only()

    def test_reset_during_evidence_scan_blocks_receipt(self):
        source, run = self.legacy(), self.evidence_run()
        self.setup_responses([source], [run], run,
                             {**ISSUE, "conversationSessionGeneration": 1})
        with self.assertRaisesRegex(NativeChatError, "conversation_session_changed"):
            self.api.reconcile("issue", "hello", "request")
        self.assert_read_only()

    def test_source_changed_during_scan_blocks_receipt(self):
        source, run = self.legacy(), self.evidence_run()
        self.setup_responses([source], [run], run, ISSUE,
                             {**source, "clientRequestId": "wrong"})
        with self.assertRaisesRegex(NativeChatError, "source_request_mismatch"):
            self.api.reconcile("issue", "hello", "request")
        self.assert_read_only()

    def test_unrelated_run_or_no_run_never_defaults_generation(self):
        source = self.legacy()
        run = self.evidence_run(
            contextSnapshot={**RUN["contextSnapshot"], "wakeCommentIds": ["other"]})
        for values in (([],), ([run], run)):
            self.setup_responses([source], *values)
            with self.assertRaisesRegex(NativeChatError, "source_generation_unverified"):
                self.api.reconcile("issue", "hello", "request")
        self.assert_read_only()

    def test_run_missing_generation_identity_or_session_fails_closed(self):
        runs = [self.evidence_run(sessionIdAfter=None), self.evidence_run(agentId="foreign"),
                self.evidence_run(contextSnapshot={**RUN["contextSnapshot"],
                                          "conversationSessionGeneration": None})]
        for run in runs:
            self.setup_responses([self.legacy()], [run], run)
            with self.assertRaises(NativeChatError):
                self.api.reconcile("issue", "hello", "request")
        self.assert_read_only()

    def test_newer_pending_run_never_reuses_older_success_session(self):
        old = self.evidence_run()
        new = self.evidence_run(id="retry", status="running", sessionIdAfter=None,
                       createdAt="2026-10-10T05:02:00.000Z")
        self.setup_responses([self.legacy()], [old, new], old, new)
        with self.assertRaisesRegex(NativeChatError, "source_run_not_successful"):
            self.api.reconcile("issue", "hello", "request")
        self.assert_read_only()

    def test_cross_generation_source_claims_are_ambiguous(self):
        old = self.evidence_run()
        new = self.evidence_run(id="retry", createdAt="2026-10-10T05:02:00.000Z",
                       contextSnapshot={**RUN["contextSnapshot"],
                                        "conversationSessionGeneration": 1})
        self.setup_responses([self.legacy()], [old, new], old, new)
        with self.assertRaisesRegex(NativeChatError, "source_generation_conflict"):
            self.api.reconcile("issue", "hello", "request")
        self.assert_read_only()

    def test_bounded_history_does_not_guess(self):
        self.setup_responses([self.legacy()], [self.evidence_run()] * 200)
        with self.assertRaisesRegex(NativeChatError, "source_evidence_window_exceeded"):
            self.api.reconcile("issue", "hello", "request")
        self.assert_read_only()

    def test_final_revalidation_detects_a_newer_retry(self):
        source, old = self.legacy(), self.evidence_run()
        new = self.evidence_run(id="retry", status="running",
                                createdAt="2026-10-10T05:02:00.000Z")
        self.setup_responses(source, [old], old, REPLY, [new, old], new, old)
        with self.assertRaisesRegex(NativeChatError, "source_run_not_successful"):
            self.api.status("issue", "source", 0)
        self.assert_read_only()
