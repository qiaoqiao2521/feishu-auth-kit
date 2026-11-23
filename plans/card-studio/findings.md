# Findings: card-studio

## Existing architecture (read 2026-09-15)

- `src/cardkit.ts` — `SingleCardRun`/`CardKitStep` snapshot model, schema
  `feishu-auth-kit.cardkit.single_card.v1`. Not a clickable card; run-summary only.
- `src/streaming-card.ts` — `StreamingCardController`: state machine
  idle→streaming→completed/error/aborted, builds CardKit 2.0 payload
  (`{config, header, elements}`), renders reasoning as lark_md quote, appends ` ▊`
  cursor while streaming. This is the natural base for the typewriter template.
- `src/flush-controller.ts` — 300ms throttle, mutex flush, reflush-on-conflict,
  `complete()` stops scheduling. Reusable by progress template.
- `src/interactive-dispatcher.ts` — `InteractiveActionDispatcher`: parses
  `card.action.trigger` shapes (`event.action.value`, `context.open_message_id`,
  `operator.open_id`...), predicate-routed handlers, returns `{toast, card}`.
  The simulator must synthesize events in exactly this shape.
- `src/native-contract.ts` — `NativeCardAction` → `NativeContinuationRecord`
  (persisted via `FileContinuationStore`, `kind=native_agent_continuation`) →
  `resolveCardActionToRetry` marks `status='confirmed'` + builds
  `NativeRetryRequest` → `buildRetryArtifactFromRequest` yields
  `SyntheticRetryArtifact` (schema `feishu-auth-kit.synthetic-retry.v1`).
  **Gap**: no idempotency — a second click re-confirms and re-emits a retry request;
  no delivered/failed transitions for timeout recovery.
- `src/runtime-cards.ts` — `RuntimeCard` (schema `feishu-auth-kit.card.v1`),
  `FileContinuationStore` with tmp+rename atomic write, `processCardAction`.
- CLI (`src/cli.ts`) — hand-rolled subcommand table (no commander); handlers
  `cmdXxx(opts): Promise<number>`, `jsonDump` for output.
- Tests are vitest colocated in `tests/*.test.ts`, mirror module names.

## Feishu CardKit 2.0 payload shape (as used in-repo)

```json
{
  "config": {"wide_screen_mode": true, "update_multi": true},
  "header": {"title": {"tag": "plain_text", "content": "..."}, "template": "blue|green|red|grey"},
  "elements": [
    {"tag": "div", "text": {"tag": "lark_md", "content": "..."}},
    {"tag": "hr"},
    {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "..."}, "type": "primary|default|danger", "value": {...}}]},
    {"tag": "note", "elements": [{"tag": "plain_text", "content": "..."}]}
  ]
}
```
Action callback values are free-form objects; repo convention is to carry
`operation_id` + `action` (+ selection data).

## Repo state notes

- Uncommitted Python-side diff swaps continuation-store writes to a local
  `atomic_json_io` package — unrelated to TS work, must be preserved.
- No repo-local AGENTS.md/PROJECT.md; controlmesh-review workspace AGENTS.md governs
  the session, its planning-file convention applied here under `plans/card-studio/`.
- Node ^26, TypeScript ^7 (native tsc), tsup build, vitest 5.
