# Optional realtime Feishu transport

## Goal
Integrate the supplied, tested transport into a reusable optional kit API, preserving
existing Python and TypeScript authentication entry points. Push verified changes
on `qingxin/realtime-feishu-transport`; deployment validation belongs to Ops.

## Scope and boundaries
- Reuse auth primitives; offer WebSocket ingress and persistent HTTPS sending.
- Encrypted local storage, process-safe tenant-token cache, durable operation ledger.
- Configurable owner direct-message and single-group mention ACL; no identities in code.
- No bundled personal runtime, required model, or complete Paperclip plugin claim.
- Configuration, keys, tokens, ciphertext, transcripts, and deployment paths stay out of Git.
- Preserve concurrent repository work; no force push or unsolicited merge/release.

## Steps
1. Repository preparation and baseline checks: complete.
2. Materialize exact Library reference and verify bytes: complete.
3. Audit source and extract optional transport with compatibility tests: complete.
4. Test recovery, concurrency, ACL, token expiry, and ambiguous delivery offline: complete.
5. Document recovery/measurement boundaries: complete; staged secret scan next.
6. Commit/push task branch; verify remote commit and return deployment contract to Ops.

## Acceptance
Existing suites remain compatible. New tests exercise real failure boundaries.
An operation with uncertain delivery never automatically re-sends. No live identity
or secret enters commits. Remote head is verified independently after pushing.
Offline test success does not imply deployment success.
