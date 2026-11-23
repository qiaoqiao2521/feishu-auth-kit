# Progress: card-studio

## Session 2026-09-15

- Explored repo, confirmed TS-side scope; created task_plan/findings.
- Phase 1 (contract hardening): done — status machine (`waiting/confirmed/delivered`),
  duplicate-click guard returning recorded resolution with `duplicate: true`,
  `markRetryDelivered` / `markRetryFailed` (failed resets to waiting with
  `delivery_failures` counter). 6 new tests.
- Phase 2 (templates): done — `src/card-templates.ts` with 5 templates
  (plan/typewriter/progress/multi-select/error-retry), all button values carry
  `operation_id` + `action`; `bindCardContinuation` helper; demo registry.
  12 tests.
- Phase 3 (simulator + CLI): done — `src/card-simulator.ts` with pure ANSI
  renderer (`renderCardJson`), canonical event synthesis
  (`buildActionTriggerEvent`, round-trips through
  `InteractiveActionDispatcher.parseContext`), `CardSimulator.click` /
  `clickRepeated` (race semantics), keypress loop; CLI `card templates` /
  `card simulate`. 17 tests.
- Phase 4 (docs/exports): done — `index.ts` exports, README sections
  「Card Studio 与本地模拟器」+「幂等与投递状态机」, capability table rows.

## Errors encountered & fixed

| Error | Attempt | Resolution |
|---|---|---|
| `cli.ts` handlers couldn't see the template positional | 1 | Inject `positionals[2]` into opts at the `card` dispatch site. |
| multi-select test sequence freed a slot (untoggle) so max-clamp assertion failed | 1 | Rewrote sequence: min-guard first, then toggle a,b, clamp c, untoggle a, toggle c. |
| fake-TTY loop test pressed `down` twice, wrapping selection back to index 0 | 1 | Single `down` → index 1 (reject). |
| plan-recovery test clicked again inside the same `deliveryFailure` simulation | 1 | Recovery uses a second healthy simulation sharing the store. |
| `--click 0,0` originally applied card updates between clicks, so the duplicate hit an empty (approved) card | 1 | Added `CardSimulator.clickRepeated`: consecutive identical clicks dispatch against the stale payload, first card response wins. |

## Verification (exact results)

- `npx tsc --noEmit` → clean, no errors.
- `npx vitest run` → **22 test files, 107 tests passed** (35 new: 6
  idempotency + 12 templates + 17 simulator; pre-existing suite unchanged and green).
- `npm run build` → tsup ESM + tsc declarations, Build success.
- CLI smoke:
  - `card templates --template plan_confirmation` → CardKit JSON payload.
  - `card simulate plan_confirmation --click 0,0 --fresh-store --store …` →
    `toast[success]` then `toast[warning] 该计划已处理过（幂等保护）`, final card
    已批准, continuation `status: delivered`.
  - `card simulate error_retry --click 0 --delivery-failure --fresh-store` →
    `toast[danger]` 回滚到 waiting，卡片仍可重试（无脏状态）。
- `git status` → only intended files; pre-existing Python `atomic_json_io`
  diff (pyproject.toml, orchestration.py, runtime_cards.py, token_store.py,
  uv.lock) untouched.

## Follow-up (out of scope)

- Mirror idempotency/delivery state machine into
  `src/feishu_auth_kit/native_contract.py` when the Python side syncs.
