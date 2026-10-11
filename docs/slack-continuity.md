# Slack owner DM → native Agent Chat

This Python-only transport is additive. Existing Feishu Python/TypeScript entry points
are unchanged. There is no per-message `sessions.create` path and no second agent
scheduler. Paperclip owns context, run scheduling, task issues and execution.

## Explicit pairing and prerequisites

One ledger binds one `(workspace, Slack app, owner user, owner DM)` to one
`(loopback Paperclip server, company, agent, local-board)` tuple. Pair only the actual
Board owner. Do not map arbitrary Slack senders to Board privileges. Do not start
another Slack/Feishu bridge or ledger targeting the same native tuple: native Agent
Chat is unique per company/agent/Board user, not per external channel or thread.
Changing any binding field requires deliberate operator migration; the existing ledger
fails closed. Keep the ledger on durable local storage and back it up while stopped.
It contains private message bodies and must not be committed or shared.

Requires an already configured Slack app/bot and existing authorized credentials.
This code does not create apps, install scopes, subscribe events, change permissions,
start a public tunnel, or deploy anything. Slack Events API must already reach an
operator-managed HTTPS reverse proxy forwarding the original raw body and headers to
`127.0.0.1:8788/slack/events`. The local listener is not an internet-facing web server.
Use a body-size limit and request timeout at that proxy.

The Paperclip reference is ControlMesh commit
[`20558b76`](https://github.com/qiaoqiao2521/ControlMesh/blob/20558b76cbb99aaee6205626fee3a32d599122ce/plugins/feishu-connector/native-conversation-cli.py)
and its recorded Paperclip 2026.916.1 acceptance. Only literal loopback HTTP addresses
with explicit ports, `local_trusted`, authenticated `local-board`, and enabled Agent
Chat are accepted. Hosted/multi-user Bearer authentication is deliberately unsupported.
The kit does not contain Paperclip core; deployment needs compatibility acceptance
against the installed version. No live Paperclip, Slack or paid model calls were used
in the repository tests.

## Run

Copy `examples/slack-binding.json` and replace placeholders with explicitly verified
identities. Do not put credentials in that file. Use the same binding and state path
for both processes. The state file must be in an owner-only directory; new CLI files
use umask 077. Start only one pump per state file (enforced with a process file lock).

```sh
# Existing credentials must be supplied through your authorized secret environment.
# Never paste or commit them. Install the Python package using your normal environment.
feishu-slack-bridge --binding /secure/slack-binding.json --state /secure/slack.sqlite serve
feishu-slack-bridge --binding /secure/slack-binding.json --state /secure/slack.sqlite pump --loop
feishu-slack-bridge --binding /secure/slack-binding.json --state /secure/slack.sqlite status
```

`serve` needs `SLACK_SIGNING_SECRET`; `pump` needs `SLACK_BOT_TOKEN`. `status` needs neither.
The ingress verifies Slack v0 HMAC, ±5-minute timestamp, app/workspace/owner/DM and
plain user message shape. A signed retry is ACKed from its durable existing receipt;
a stale replay is rejected. No bot messages, edits or third-party senders are admitted.
The source Slack message timestamp contributes only to a stable `clientRequestId`.
It never becomes a native conversation identifier. A signed challenge is returned
only for URL verification; it is not persisted as a user message.

Ingress persists the receipt before ACK and performs no native or Slack API call.
The pump does bounded HTTP transport passes, not agent scheduling. Library users
must likewise serialize pump calls; the CLI enforces this across processes.

## Continuity and `/new`

The first native get/create result is stored as `conversationIssueId`; all ordinary
messages append comments to this issue. Mention task issue IDs explicitly in the
message text (for example, “continue task ABC-123”). They are passed unchanged to the
native coordinator; the transport never silently retargets the DM. Completing an
execution task does not reset the coordinating conversation.

Only an explicit message whose trimmed text is `/new` creates a local generation
fence and submits the native `/new` comment. Native confirmation must retain the
same issue and report the new `conversationSessionGeneration`. Old comments remain
in native history. A later message waits for its preceding reset confirmation.
Older unsent messages/results are suppressed when a new generation is received;
an already in-flight Slack send cannot be revoked. Reset completion is visible in
`status`; the transport does not invent an agent-final comment as acknowledgement.

A successful reply must belong to the bound issue, agent and source comment, be from
the selected successful run, and match that run's explicit
`paperclip.run_presentation_decision.v1` comment selection. Progress comments,
failed-run output, mismatched generations and unselected comments are not delivered.
Native interaction approvals/questions remain in Paperclip UI; this transport does
not accept approvals or impersonate an interaction response through Slack.

## Crash recovery and truthful receipts

The ledger separately records receipt, native execution and Slack delivery. Intent
is committed before either outbound write. A timeout, crash or unvalidated response
leaves `unknown`. On restart, native unknowns use GET-only reconciliation. No automatic
POST retry occurs even when a bounded lookup finds no match. An unknown Slack send
is never repeated automatically; successful delivery requires `ok=true`, the exact
channel and a real message timestamp. Coalesced native replies are deduplicated by
`conversationIssueId + replyCommentId`, including across source messages/restarts.

`unknown` is an operator-visible blocker, not a success or failure claim. Inspect the
native comment/run or Slack history read-only before deciding what happened. The CLI
provides no force resend/reset-ledger command. Do not delete the ledger to “retry”:
that discards deduplication evidence and can duplicate execution/delivery. Failed runs
are recorded without sending their comments. Source history/run scans are bounded;
insufficient evidence fails closed. This intentionally favors safety over availability.

## Verification

```sh
python -m pytest -q
ruff check src/feishu_auth_kit/slack*.py tests/test_slack*.py
npm test
npm run typecheck
npm run build
```

Tests use deterministic local fakes/mocked HTTP, including owner/signature rejection,
replay handling, stable conversation context, generation fences, task references,
uncertain writes, crash recovery, coalesced results and strict final provenance.
Server deployment, real Slack DM continuity and installed-Paperclip compatibility
remain explicit post-deployment acceptance steps, not claimed by unit tests.

### Ordering and send boundary

The latest reset's Slack source timestamp is stored using exact decimal comparison.
A delayed older source message (including an older reset) is receipted as superseded,
not injected into the new native generation. Consecutive not-yet-submitted resets may
be superseded by the newest reset; they do not cause multiple unnecessary native resets.
The durable Slack delivery claim is the send linearization boundary: after that claim,
the request is in flight and a concurrently received `/new` cannot revoke it. No
exactly-once network delivery or recall is promised. Before the claim, generation
checking and claim insertion are in the same SQLite transaction.

Unknown Slack delivery uses read-only `conversations.history` with the existing token,
matching the exact stable `client_msg_id`, channel and complete text. Missing existing
history scope, truncated lookup or absent/ambiguous identifiers leaves it unknown;
the bridge never adds scopes or resends. `status` exposes that unresolved receipt for
operator inspection. This is recovery of evidence, not automatic retry.

Ledger schema version 1 is checked on open. Earlier experimental ledgers missing
reset-fence columns and unknown future versions are rejected with an explicit
migration-required error; records are never dropped or silently rebuilt. This first
published version does not automatically migrate experimental databases. Preserve
and back up any candidate ledger, reconcile its outstanding operations, and arrange
an explicit migration before starting this version against it. A compatible unversioned
candidate ledger may be marked version 1 without changing its records.

### Paperclip 2026.916.1 ordinary-comment generation compatibility

This installed version leaves ordinary owner comments' `conversationSessionGeneration`
null. It records the authoritative generation on the run context; `/new` separately
writes its generation onto the reset command comment when the native reset is processed.
The ControlMesh reference commit is not the kit deployment commit.

For an explicitly null ordinary-comment generation, the adapter now uses GET-only
recovery: validate the bound Board owner/company/issue, exact source comment and
clientRequestId/text; inspect a bounded, unsaturated run window; require matching
source-comment linkage, one consistent generation, an unambiguous latest successful
run and a nonempty native/provider session receipt. It then re-reads the conversation
and source to reject an intervening reset or source change. Missing fields, conflicting
generations, pending/newer retries, missing session evidence and truncated windows
remain unknown. Generation never defaults to zero or comes from the current issue.

The raw comment retains null. A separate `generationEvidence` receipt carries the
verified source/run/session chain; the existing ledger stores its source ID, verified
native generation and run ID. No schema migration or historical record rewrite is
needed. Before returning a final reply, the API revalidates that evidence against the
selected successful run and explicit presentation decision. `/new` without its own
persisted generation remains unknown until a later read sees that exact reset receipt.
Existing local reset fences still suppress unclaimed older replies. A remote reset
can race after the last read; this does not promise a distributed atomic send/reset
boundary or recall of a network send already claimed.

Verification used offline source/run fixtures and restart/duplicate/reset/send-failure
tests. A read-only check against two existing production receipts resolved both to
their original final comments without writing the ledger, sending Slack messages or
starting runs. Deployment must preserve the existing ledger and ingress. The first
approved pump pass can deliver already-computed results; it must not resubmit their
source messages. No acceptance marker is created by this compatibility update.
