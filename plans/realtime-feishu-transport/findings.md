# Findings

- Clean remote baseline: `77cf53d4233d7d9b2ef38481ca4c30f00ca386a4`.
- Effective GitHub CLI account confirmed as repository owner `qiaoqiao2521`.
- Existing primary checkout has unfinished card-studio changes and atomic JSON
  storage changes; preserved. Its origin still names the archived account.
- The isolated checkout uses the specified owner's repository for fetch and push.
- No repository-local AGENTS, PROJECT, or skills were present in baseline.
- Current auth client accepts an injected HTTP session; reuse this seam instead of
  copying authentication. Its in-memory tenant-token cache lacks expiry tracking.
- Existing FileTokenStore is plaintext and has no process lock. Introduce a separate
  optional encrypted store rather than silently changing its persistence format.
- Existing message-context normalization provides a suitable post-ACL envelope.
- README currently excludes sending/ingress: document the optional transport boundary
  once the actual package is reviewed, while leaving host scheduling/session ownership clear.

## Proposed integration, pending package review
- Optional transport module with explicit configuration and lifecycle.
- SDK WebSocket adapter hands events to ACL, event deduplication, and existing normalization;
  host callback owns execution and response scheduling.
- Process lock plus atomic replacement around encrypted token-cache refresh; expiry skew
  and application/domain namespacing prevent stale or cross-app token reuse.
- Durable operation transitions: reserved -> sending -> succeeded / failed / unknown.
  Reserve before network dispatch; interrupted sending recovers as unknown. Unknown
  delivery requires explicit reconciliation, never automatic replay.
- Persistent HTTPS session is injected/configurable, with finite timeouts and teardown.
  Deterministic platform rejection is distinct from timeout/disconnect after dispatch.
- ACL derives sender/chat/mentions from structured events, never text substrings.
- Runtime state location/key provision comes from host configuration; no hardcoded host paths.
- Reuse SDK dependencies optionally; do not vendor full third-party source trees.

## Measurement boundary
Reported prior cloud observations: inbound 0.5–0.7 seconds; send 0.7–1.4 seconds.
These have not been independently repeated in this checkout. Full response time
includes host scheduling and model execution; transport timing is not an end-to-end guarantee.

## Integration decisions adopted
- Separate host-profile auth from transport state: injected OperationStore may hold
  routing/operation metadata without storing provider credentials or tokens.
- Canonical binding is versioned and explicit; legacy diagnostic names are fallback
  readers only. Profile attestation is a host verification boundary, not discovery.
- Host inbox and watermarks share one encrypted record update. Explicit recovery
  can replay callback effects, so message-ID/operation-key dedup remains necessary.
- Retain raw thread/root/parent IDs and include thread-reply semantics in fingerprints.
- Apply scoped verification, rather than equating offline success with deployment:
  adopted from Obsidian `Wiki/开发知识入口.md` acceptance guidance and task constraints.
- Idle send sample (8.262 s with 0 ms auth) disproves stable subsecond POST claims.
  The supplied subsequent single-group @/reply evidence validates the original cloud
  runtime only; destination-host packaged revision still needs Ops validation.
