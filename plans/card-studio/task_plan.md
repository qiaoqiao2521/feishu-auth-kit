# Task Plan: Feishu CardKit Interactive Studio & CLI Simulator

## Goal

Give feishu-auth-kit a zero-network local development loop for interactive cards:

1. A local CLI simulator that renders Feishu CardKit 2.0 JSON cards in the terminal
   and simulates user clicks as `card.action.trigger` callbacks through
   `InteractiveActionDispatcher` — no webhook, no public network, no real Feishu client.
2. Five plug-and-play card templates (plan confirmation, streaming typewriter,
   progress tracking, multi-select decision, error retry), each emitting standard
   CardKit JSON and binding every button to a native continuation operation_id.
3. Hardened action -> continuation -> retry contract: idempotent clicks (double-click /
   duplicate callback never produces a second retry artifact), explicit delivery
   state machine (waiting -> confirmed -> delivered, failed -> waiting) so timeouts
   never leave a card in a dirty state.

Non-goals: no network listener, no real card send, no new runtime dependencies,
no Python-side port in this task (tracked as follow-up).

## Repo Constraints

- Flat `src/*.ts` module style, ESM (`.js` import suffixes), schema strings
  `feishu-auth-kit.<thing>.v1`.
- Dev deps are only tsup/typescript/vitest/@types/node — keep it that way
  (no Ink; hand-rolled ANSI renderer).
- Preserve existing uncommitted Python changes (atomic_json_io refactor).
- Existing `resolveCardActionToRetry` tests must keep passing.

## Phases

### Phase 1: Contract hardening — Status: complete
- `src/native-contract.ts`: status state machine (`waiting`/`confirmed`/`delivered`),
  duplicate-click guard keyed on `(operationId, action, actorOpenId)` idempotency key,
  `markRetryDelivered`, `markRetryFailed`, `ResolvedNativeAction.duplicate` flag.
- Tests: `tests/native-contract-idempotency.test.ts` (plus existing suite green).

### Phase 2: Card templates — Status: complete
- `src/card-templates.ts`: builders + small state controllers for the 5 templates.
  All button values carry `operation_id` + `action`; templates emit CardKit 2.0
  `{config, header, elements}` payloads and also `RuntimeCard`-style dicts where useful.
- Tests: `tests/card-templates.test.ts`.

### Phase 3: Simulator — Status: complete
- `src/card-simulator.ts`:
  - `renderCardJson(payload, opts)` — pure JSON-to-ANSI text renderer (header/div/lark_md
    subset/hr/action buttons/note/img placeholder), testable string output.
  - `buildActionTriggerEvent(card, actionIndex, actor)` — synthesizes canonical
    `card.action.trigger` event from a card payload.
  - `CardSimulator` — interactive keypress loop (up/down/enter/q), dispatches clicks
    through `InteractiveActionDispatcher`, applies `{card}` responses in place, prints
    toasts; `--print`/one-shot mode for CI.
- CLI: `card-templates` (dump template JSON), `card-simulate` (run one template or a
  JSON file; `--click <n>` for non-interactive click simulation).
- Tests: `tests/card-simulator.test.ts`.

### Phase 4: Docs & exports — Status: complete
- `src/index.ts` exports, README "Card Studio & Simulator" section,
  this plan + findings + progress kept current.

## Decisions Made

| Decision | Why |
|---|---|
| Hand-rolled ANSI renderer instead of Ink | Zero new deps (repo constraint); renderer must also be a pure testable function, which Ink complicates. |
| Templates in one `card-templates.ts` module | Matches flat module style; each template is a small exported factory. |
| Idempotency via stored `resolved_action` on the continuation | No schema migration of store shape needed (payload is open); duplicates detectable across processes since store is the shared file. |
| No Python port in this task | Task brief targets the TS product layer; Python sync listed as follow-up. |

## Errors Encountered

| Error | Attempt | Resolution |
|---|---|---|
| CLI positional args not visible to `card` handlers | 1 | Inject `positionals[2]` as `--template` at dispatch site. |
| `--click 0,0` applied updates between clicks, duplicate hit empty approved card | 1 | Added `CardSimulator.clickRepeated` race semantics (stale-payload group dispatch). |
| Test-sequence bugs (multi-select untoggle, TTY double-down wrap) | 1 | Fixed test logic; see progress.md error log. |

## Next Step

All phases complete and verified (107/107 tests, typecheck clean, build OK).
Follow-up (out of scope): mirror the idempotency/delivery state machine into
`src/feishu_auth_kit/native_contract.py` when Python side syncs.
