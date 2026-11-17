# Optional realtime transport

Install `uv sync --extra transport --extra dev` or `pip install '.[transport]'`.
Existing Python/TypeScript auth APIs are unchanged. The transport is a Python,
Linux-only optional API; it is not a complete Paperclip plugin. Host execution,
session routing, ordering, deployment and process supervision remain host-owned.
The WebSocket worker requires Linux flock/inotify and the pinned lark-oapi 1.7.3 /
websockets 15.0.1 versions: it uses private SDK connection/loop hooks. Run it in a
dedicated process; upgrading the SDK requires validating those hooks.

## Normal authenticated CLI execution (no credential extraction)

When the host's normal official CLI already owns its encrypted profile, prefer
`TransportSender(store, request_executor=backend, timeout=20)`. No token provider
is called, no profile file is read/decrypted, and no app secret or tenant token
is exported. The backend implements `RequestExecutor`:

- `backend.app_id`: the existing independently verified profile app.
- `backend.request(method, url, **kwargs)`: invoke the normal authorized CLI/API
  execution path using that existing profile. Parameters include JSON body,
  query params, timeout and redirect policy; `headers` is empty in executor mode.
- Return an object with `status_code` and `json()` exposing the ordinary Feishu
  response envelope. Adapt the normal CLI response shape in the host backend.
- Preserve JSON `uuid`, text, reply endpoint and `reply_in_thread` exactly; bound
  subprocess time, disable message-send retries, and propagate timeout/disconnect.
  Never read/decrypt auth config or print raw CLI outputs/credentials.

The kit persists the operation/UUID before calling request, and maps executor
interruption or uncertainty to `unknown`. Auth rejection is `failed` without
replaying the message. The host chooses its CLI's supported API syntax; no
unverified third-party command is hardcoded in this kit.

```python
# backend calls the host's normal official CLI with its existing profile.
sender = TransportSender(store, request_executor=backend, timeout=20)
result = sender.send("stable-operation", "Synthetic reply",
                     reply_to="om_SYNTHETICREAD", reply_in_thread=True)
```

```bash
feishu-transport send --executor host_adapter:request_executor \
  --store-factory host_adapter:operation_store --operation stable-operation \
  --text-file /tmp/synthetic-reply.txt --reply-to om_SYNTHETICREAD --reply-in-thread
```

`--executor` and `--provider` are mutually exclusive. This boundary reuses the
host's authorized CLI authentication; it does not bypass a denied low-level
credential/profile read. No new bot, identity switch or credential migration is
needed. WebSocket credentials remain a separate capability: executor mode does
not extract them to start a new SDK connection.

## Existing host CLI/profile authentication

No registration, profile switching, cloud credentials or cloud encryption key
are required by `TransportSender`. Supply a provider for the already verified
host profile:

- `provider.app_id`: that profile's app-scoped identity.
- `provider.get_tenant_access_token(force_refresh=False)`: returns an object with
  `token` and `expire` (remaining seconds). A kit `FeishuAuthClient` also fits.
- The host adapter may invoke its existing authenticated CLI, with a bounded
  timeout; parse tokens internally and never print tokens or subprocess output.
  The kit supplies the interface, not a command for an unspecified third-party CLI.
- This provider owns its token cache/storage. `TransportSender` does not copy
  provider credentials into kit state or require the encrypted tenant cache.

`OperationStore` is an injectable durable metadata store with `private: Path`,
`read(name)`, `write(name, value)`, and `exists(name)`. `private` is a host-created
0700 directory used for process locks. Writes must be atomic and fsync both file
and directory before returning. It must protect routing/operation records from
untrusted modification. This is independent of the provider's auth storage.
`EncryptedStore` is one optional implementation. Its `initialize()` creates a
local at-rest key only, never an app or authentication credential. Keep its key,
records, SQLite queue, local attachment directories and config outside Git.

```python
import requests
from feishu_auth_kit.transport import (
    TransportBinding, TransportSender, bind_verified_profile,
)

# store and existing_provider are supplied by the host. Before calling this,
# independently verify the current profile's app, owner and private conversation.
# All identifiers below are synthetic, never deployment configuration.
binding = TransportBinding(
    app_id="cli_SYNTHETICAPP", owner_open_id="ou_SYNTHETICOWNER",
    private_chat_id="oc_SYNTHETICPRIVATE",
)
# One-time metadata binding only; refuses existing state rather than replacing it.
bind_verified_profile(store, binding, brand="feishu")

session = requests.Session()  # Keep it alive for the host worker's lifetime.
session.trust_env = False  # Opt in to a trusted environment proxy deliberately.
sender = TransportSender(store, auth_provider=existing_provider, session=session, timeout=20)
result = sender.send(
    "stable-host-operation-key", "Synthetic reply", reply_to="om_SYNTHETICREAD",
    reply_in_thread=True,
)
# reply_to must already have a verified private read receipt, or a group receipt
# paired with explicit target_chat. A fresh DM can omit reply_to.
# session.close() at worker shutdown.
```

`bind_verified_profile` trusts the host's independent verification; it is not
identity discovery. App-scoped owner IDs can differ between apps. Preserve the
host's own verified identity. An alternative `configure_binding` performs
read-only authenticated app-owner and private-chat lookups for a kit auth client;
it does not send a diagnostic image, create a bot, or fabricate successful tests.

The installed CLI is `feishu-transport` (also Python module
`feishu_auth_kit.transport.cli`). Host factory modules stay outside the repository:

```bash
feishu-transport send --provider host_adapter:token_provider \
  --store-factory host_adapter:operation_store --operation stable-host-operation-key \
  --text-file /tmp/synthetic-reply.txt --reply-to om_SYNTHETICREAD --reply-in-thread
feishu-transport status --store-factory host_adapter:operation_store \
  --operation stable-host-operation-key
```

Factories take no arguments and use the host's secure configuration. `--state-dir`
selects the optional encrypted-store implementation when `--store-factory` is
absent. CLI output excludes reply text, provider tokens and remote error bodies.
Failed attempted requests retain their own status/error; prior delivery metadata
is returned separately as `operation_status`, never substituted for current success.
`--reply-in-thread` defaults to false, requires `reply_to`, and participates in
the operation fingerprint alongside text, destination and reply target. Changing
any of these under an existing operation is rejected before another POST.

## Durable send states and host 30-second timeout

| Public status | Meaning | Recovery |
|---|---|---|
| `pending` | Outbox request is durably queued; result is not yet recorded | Inspect/wait on the same operation; never invent a new key to retry |
| `sent` | Platform message ID recorded durably | Return existing result; do not resend |
| `unknown` | Dispatch began, worker was interrupted, response was ambiguous, or result could not be recorded | Fence old worker, inspect operation UUID and platform receipt; no automatic resend |
| `failed` | Auth failed before message POST, explicit auth rejection, or no registered operation | Fix the cause; an actual failed delivery requires explicit authorization for a new attempt/key |

The operation UUID and request fingerprint are persisted before token acquisition
or POST. A repeated operation returns its previous result without sending again.
Internal `sending` and target-mismatch records map to public `unknown`; they do
not prove a retry is safe. Public `TransportSender.send/status` and the persistent
outbox normalize this contract. The legacy low-level `chat_io.send_once` retains
some internal status names for prototype diagnostics; use the public facade in hosts.

`TransportSender` uses a default 20-second HTTP timeout, below a 30-second host CLI
budget. The provider should have its own short bound (for example 5 seconds).
Requests timeouts are connect/read limits, not a hard end-to-end deadline. If the
host kills a process after 30 seconds, query `status(operation)`; unfinished
`sending` is `unknown`. Do not treat CLI exit/timeout as permission to send again.
No background keepalive or extra network polling is installed.

`sender.reconcile(operation, message_id=..., chat_id=...)` only records a receipt
which the host independently proved belongs to this operation UUID and target.
It performs no network send. Absence of a receipt does not establish non-delivery.
The host must stop/fence an earlier worker before recovery. A successful callback
or unit test cannot promise exactly-once external effects.

## Ingress, ACL and durable host intake

The official authenticated SDK dispatcher feeds `EventQueue.receive`; never wire
raw public JSON directly into it. Private ingress requires app + owner + verified
private chat. Group ingress requires that same owner in exactly one configured
group, with a structured mention of the authenticated bot Open ID. Other bots,
users, groups and text-only mention lookalikes are ignored. Group policy creation
uses read-only bot-info lookup; it does not grant scopes or change membership.

`normalize_owner_event` and callback messages retain the raw `thread_id`, `root_id`
and `parent_id` for stable host routing; absent values remain absent/null. Do not
substitute a mutable latest issue/message source for these identifiers.
Group messages carry `reply_context` with `scope=group_only`, `allow_dm_context=False`,
`target_chat_id` and `reply_to`. The host must enforce it and supply both explicit
target_chat and the read owner-mention reply_to for group sends. Do not add private
chat memory to group prompts or replies.

`read_queued` writes the message-ID watermark AND encrypted `host_inbox` entry in
one atomic binding-record update. `consume_once(store, after=..., on_message=...)`
delivers pending intake to a host callback. The callback must durably accept
custody before returning; expensive model work can run in the host's own worker.
Failed callbacks remain `failed`; process interruption can leave `processing`.
`inbox_status` exposes status/attempt counts only. `recover_delivery` explicitly
resets a failed/interrupted entry to pending after the old worker is fenced;
`deliver_pending` then retries. Callback effects may have occurred before a crash:
use message-ID deduplication and stable outbound operation keys. Never claim
exactly-once callback processing. Pending batches survive host checkpoint loss.

`read_queued/consume_once` accept an optional auth_provider for attachment access.
`listen(..., credentials_provider=...)` can fetch matching existing app credentials
in memory from the host for the official SDK without persisting them. A tenant
bearer token alone cannot authenticate the SDK WebSocket connection. Listener and
persistent sender must each have one supervised process. `outbound_io.serve`
accepts auth_provider for its persistent HTTPS session; `submit` queues exact
caller-supplied text, never automatic model output.

## Compatibility and recovery limits

Legacy binding keys `overview_delivery.chat_id`, `receive_test.baseline_ids`,
`name_matches`, `owner_matches` remain readable. New `TransportBinding` uses
explicit private_chat_id and baseline_message_ids, with a verified versioned
binding. Existing state is never silently rebound or overwritten. Legacy read
watermarks cannot reconstruct callbacks already lost before durable inbox existed.
Operator-controlled migration needs original keys and independently verified facts.

The optional encrypted tenant cache uses app/domain-isolated entries, expiry skew,
clock-rollback invalidation and process-wide single-flight refresh. Provider mode
can use its own profile cache. Authentication rejection does not replay POST.
Fernet protects payloads at rest; metadata/identifiers in private SQLite and status
files are not encrypted, and possession of both key and ciphertext defeats this
at-rest protection. The prototype file upload/send primitive has no durable file
operation ledger: do not expose it as retryable file delivery without a host wrapper.
Queue, inbox and operation history need host-defined retention; automatic pruning
could break deduplication and is deliberately absent.

## Evidence and latency boundaries

The supplied 28-file handoff SHA256 was
`368f0d1d365ecfb5ee93effaf76250ca591ef3919cd9d008f05e3715f55ff75e`.
It builds on kit baseline `77cf53d4233d7d9b2ef38481ca4c30f00ca386a4`.
Only adapter modules/tests were integrated; no upstream tree, cloud state,
identities, attachments, secrets or staging machinery were vendored.

Prior cloud evidence supplied by the integration owner: owner private messages
and files worked bidirectionally; warm active POST samples took 0.74–1.45 seconds,
and private ingress samples about 0.5–0.7 seconds. After roughly ten idle minutes,
one actual POST took 8.262 seconds while auth cache time remained 0 ms. Connection
cooling/proxy/service latency is a possibility, not an independently established
cause. Cache and connection reuse gains are observations, never an SLA.
Subsequently the original cloud runtime received an owner mention in the single
authorized group in 955 ms and replied to the same group in 1235 ms with matching
returned chat. No real identifiers or conversation content accompany that evidence.

Those cloud observations do not validate this packaged revision or deployment.
Forced disconnect/recovery, switch-history backfill and destination-host live
operation remain unverified here. Full answers still depend on host scheduling
and model work. Offline tests cover ACL/deduplication, encryption, thread routing,
callback recovery, process cache locking, concurrent operation reservation,
ambiguous POST outcomes and existing-profile CLI injection, with network blocked.
