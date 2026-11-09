# COVERAGE.md — evidence per acceptance row (BUILD_SPEC §6)

Build window: 2026-11-09 ~19:40 UTC → 2026-11-10 (overnight build).
Environment: isolated harness (`~/Projects/hermes-private-mode/harness/`), fake
local OpenAI-compatible provider (SSE + non-SSE, captures every wire request),
real state.db, real gateway (auto-spawned per CLI turn), zero real-provider
traffic except two disclosed 401 probes (row 4 aux observation).

Legend: ✅ e2e-verified · 🧪 unit/contract-verified · ⏸️ blocked (reason)

| Row | Area | Status | Evidence (cmd + result) |
|---|---|---|---|
| 1 | Catalog validation | ✅ | `hermes plugins validate --install-deps` → "Validation passed." Capabilities exact; security scan safe; one disclosed warning (`save_plugin_setting`, justified in BUILD_LOG §imports). |
| 2 | Unit tests | ✅ | `pytest tests/unit` → **61 passed** (settings/platform scope, store flock ×200 increments, routes precedence + worst-verdict + confirmations, copy-on-write ×3 shapes, strip ×3 + spans, vetoes, synthetic shapes, error mapping, log filter, lifecycle state machine incl. arming span + automatic-set + neighbour safety + keep). |
| 3 | Contract pins | ✅ | `pytest tests/contract` → **19 passed**: memory markers vs `tools/memory_tool_store.py`, `<memory-context>` vs `agent/memory_context.py`, Claude Code prefix, both log formats, `_AUTOMATIC_SESSION_END_REASONS`, end-reason literals scanned across core (unknown set `{'adopted_by_profile'}` reviewed + documented), delete-fence signature, AST check of the single `delete_session` call (`sessions_dir=None`, `expected_delete_ids=targets`, `exclude_active_write_guards=True`, no compression-chain), writer file naming vs `_safe_session_filename_component`, dump-name collision regex, `store: False` in codex transport, config_schema≡DEFAULTS, capabilities≡registrations, no subprocess/os.system anywhere. |
| 4 | Blocked-route synthetic (3 api modes) | ✅ chat_completions e2e; 🧪 other modes | `chat -q` with main route = open.bigmodel.cn → assistant turn = `⛔ Private mode blocked this request to zhipu-cn: route trains with no opt-out. Background calls (e.g. title) may already have run…`; provider never answered; disclosed aux attempt observed live (title call → 401). Synthetic shapes for anthropic_messages/codex_responses unit-verified (`openai.types.*` construction); non-supported modes → None (fail closed) unit-verified. |
| 5 | Memory strip | ✅ non-vacuous | Control (plugin disabled, fresh gateway): **1 of 6** wire requests carries `MEMORY (your personal notes) [3% — 71/2,200 chars]` + sentinel entry. Protected: **0 of 16** requests carry any memory marker while the same MEMORY.md/USER.md sentinels are on disk and (post-strip) absent from every outgoing request. Copy-on-write asserted in unit tests. |
| 6 | Tool vetoes | 🧪 | Unit: `memory`/`skill_manage`/`session_search`/external-provider vetoes; `/private on` forces private in mode off; pass-through for unrelated tools. Live veto attempt blocked by expired harness OAuth (⏸️ row 22) — the veto path is a synchronous pre-tool hook, unit-verified. |
| 7 | CLI quit | ✅ | One-shot `chat -q` exits → row ends `cli_close`; post-consent sessions deleted on next activity (see row 9). |
| 8 | `/new` | 🧪 | Source pin: `new_session` written by `hermes_cli/cli_session_mixin.py` (contract test); allowlist covers it; interactive pty automation deferred (TUI input automation — disclosed in README). |
| 9 | Fenced delete e2e | ✅ | Post-consent closed chats deleted via the fence: **0 rows, 0 messages, 0 FTS hits, 0 files** (`sessions|0`, `messages|0`, no dump files, WAL `busy=0`); `last_sweep: deleted=1`; `status` shows `Sessions: deleted=4, excluded=1`. |
| 10 | TUI close | 🧪 | `tui_close` in allowlist + source pin (tui_gateway/session_lifecycle.py); same finalize path as CLI. |
| 11 | Automatic reasons leave rows | ✅/🧪 | Unit: `ws_disconnect`-closed row survives; e2e row 20 (open row survives kill) proves the closed-only rule live. |
| 12 | Blocked-route aux disclosure | ✅ | Live observation during row 4: aux title call attempted against the blocked route independently (401) — the banner's "Background calls may already have run" is literally true; `status` line 2 discloses non-enforcement. |
| 13 | Pre-setup chats survive | ✅ | Pre-consent chat closed and survived all later sweeps (`row16 survives: 1`); ledger shows `excluded: cleanup not enrolled` for 13 pre-consent sessions. |
| 14 | Delegate subagents | 🧪 | Unit: delegate child (model_config `_delegate_from`) with no ledger entry refuses the whole fenced delete; parents/children verified untouched in the real-schema DB. |
| 15 | `/private keep` | 🧪 | Unit: kept session excluded + survives sweeps across generations. Interactive CLI ack deferred (see row 8). |
| 16 | No consent → nothing ever deleted | ✅ | Every pre-consent session in the live harness ledger is `excluded: cleanup not enrolled`; sweeps ran (`last_sweep` recorded) and deleted 0 of them. |
| 17 | Neighbour-safety (abc/abc_extra) | ✅/🧪 | Contract: dump-name timestamp regex cannot match neighbour dumps (exact-name corpus test); unit: neighbour rows never touched; e2e: 29 unrelated session files intact post-sweep. |
| 18 | Profile switches | 🧪 | Unit: `_plugin_home_key`-style per-home state (store paths derive from captured home); generation requirement excludes sessions born pre-switch. |
| 19 | Log redaction | ✅ non-vacuous | Sentinel message flowed (assistant reply returned; db row existed pre-sweep) yet **0 occurrences** across agent.log, gateway.log, tool_calls.log; `[redacted]` visible on the inbound/turn lines in agent.log. |
| 20 | Crash mid-session | ✅ | Gateway `kill -9` with a turn in flight → row stays **open with `<null>` end_reason**, survives the next sweep; no invented close. |
| 21 | Status coverage | ✅ | `hermes incognito-private-mode status` renders mode, 3 coverage lines (⛔/⚠️/machine), consent state, arming generation, session counts, last sweep, deep-clean reminder, "What stays", command list. |
| 22 | Live GLM pass-through + labels | ⏸️ blocked | Harness Z.ai OAuth expired (401 token expired; obtained 2026-11-09 20:19 UTC, 1 h expiry). All harness provider tokens are OAuth with ≤1 h TTL; no static keys exist in the harness env. Owner re-login required; flags/labels themselves verified at dispatch level (see row 23 evidence) and row 4 proved live route classification + block. |
| 23 | Live Nous + provider.zdr on wire | ⏸️ blocked (same token expiry) | Flags-on-wire proven non-vacuously at dispatch level: `invoke_middleware("llm_request", provider=nous…)` → `extra_body.provider.data_collection=deny, zdr=True` on the returned request; openrouter-classified requests through the live gateway carried the same flags (row 5 protected captures include flagged requests to the custom endpoint). |
| 24 | P1 transform routes | 📋 documented | route-table rows exist (`p1_transform` kind) but transforms are not implemented in P0; classification never blesses them (label `warn` ceiling) — unit-tested (`test_p1_routes_not_blessed`). |

## Checkpoint-1 metric (rows 1–3, 6, 7–9, 13, 16, 17)

**7 of 9 fully green with e2e evidence; row 6 unit-green (live veto pending owner
re-login); row 8 source-pinned (interactive path pending).** Nothing silently
skipped: every gap above names its blocker.
