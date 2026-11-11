# COVERAGE.md — evidence per acceptance row (BUILD_SPEC §6)

Build window: 2026-11-09 ~19:40 UTC → 2026-11-10 (overnight build).
Environment: isolated harness (`~/Projects/hermes-private-mode/harness/`), fake
local OpenAI-compatible provider (SSE + non-SSE, captures every wire request),
real state.db, real gateway (auto-spawned per CLI turn), zero real-provider
traffic except two disclosed 401 probes (row 4 aux observation).

Legend: ✅ e2e-verified · 🧪 unit/contract-verified · ⏸️ blocked (reason)

**Row numbering note:** the row numbers below are this file's own and diverge
from BUILD_SPEC §6 from row 12 on. Mapping (COVERAGE → spec §6): 1–11 → 1–11;
12 (aux disclosure) → no spec row (spec 12 = compressed chat, see *Release
gaps*); 13 → 13; 14 (delegate subagents) → second half of spec 15; spec 14
(generations) has no row here (see *Release gaps*); 15 → first half of spec 15;
16–23 → 16–23; 24 (P1 transform routes) → spec 26; the live-provider matrix →
spec 25. Spec 24 (messaging fake platform) and 27 (Desktop badge) have no rows.
Rows were not renumbered so that earlier references stay valid.

**Current suite:** `pytest -q unit/ contract/` → **322 passed** (unit + contract, at HEAD 159820b; see git log)
contract) at commit c954724. Counts quoted in rows 2/3 and in dated addenda
below are historical snapshots, labelled as such.

| Row | Area | Status | Evidence (cmd + result) |
|---|---|---|---|
| 1 | Catalog validation | ✅ | `hermes plugins validate --install-deps` → "Validation passed." Capabilities exact; security scan safe; one disclosed warning (`save_plugin_setting`, justified in BUILD_LOG §imports). |
| 2 | Unit tests | ✅ | `pytest tests/unit` → **61 passed at checkpoint-1** (now 107 unit, see *Current suite*) (settings/platform scope, store flock ×200 increments, routes precedence + worst-verdict + confirmations, copy-on-write ×3 shapes, strip ×3 + spans, vetoes, synthetic shapes, error mapping, log filter, lifecycle state machine incl. arming span + automatic-set + neighbour safety + keep). |
| 3 | Contract pins | ✅ | `pytest tests/contract` → **19 passed at checkpoint-1** (now 20): memory markers vs `tools/memory_tool_store.py`, `<memory-context>` vs `agent/memory_context.py`, Claude Code prefix, both log formats, `_AUTOMATIC_SESSION_END_REASONS`, end-reason literals scanned across core (unknown set `{'adopted_by_profile'}` reviewed + documented), delete-fence signature, AST check of the single `delete_session` call (`sessions_dir=None`, `expected_delete_ids=targets`, `exclude_active_write_guards=True`, no compression-chain), writer file naming vs `_safe_session_filename_component`, dump-name collision regex, `store: False` in codex transport, config_schema≡DEFAULTS, capabilities≡registrations, no subprocess/os.system anywhere. |
| 4 | Blocked-route synthetic (3 api modes) | ✅ chat_completions e2e; 🧪 other modes | `chat -q` with main route = open.bigmodel.cn → assistant turn = `⛔ Private mode blocked this request to zhipu-cn: route trains with no opt-out. Background calls (e.g. title) may already have run…`; provider never answered; disclosed aux attempt observed live (title call → 401). Synthetic shapes for anthropic_messages/codex_responses unit-verified (`openai.types.*` construction); non-supported modes → None (fail closed) unit-verified. |
| 5 | Memory strip | ✅ non-vacuous | Control (plugin disabled, fresh gateway): **1 of 6** wire requests carries `MEMORY (your personal notes) [3% — 71/2,200 chars]` + sentinel entry. Protected: **0 of 16** requests carry any memory marker while the same MEMORY.md/USER.md sentinels are on disk and (post-strip) absent from every outgoing request. Copy-on-write asserted in unit tests. |
| 6 | Tool vetoes | ✅ `memory` live; 🧪 others | Live: `memory` veto observed with real providers (Nous, DeepSeek, OpenRouter, Codex, MiMo; see row 23 and the live-provider matrix): tool call attempted → vetoed (Nous/DeepSeek runs: MEMORY.md md5 unchanged, canary absent). Unit only: `skill_manage`/`session_search`/external-provider vetoes, `/private on` forcing private in mode off, pass-through for unrelated tools. USER.md/skills-dir unchanged checks were not recorded live. (The earlier "live attempt blocked by expired OAuth" note was superseded after the owner login round.) |
| 7 | CLI quit | ✅ | One-shot `chat -q` exits → row ends `cli_close`; post-consent sessions deleted on next activity (see row 9). |
| 8 | `/new` | ⏸️ delivery-only skip | Removal mechanism = row 7/9 path, e2e-proven (armed + cli_close + consented → swept on next activity; new chat untouched). The `/new` keystroke needs the interactive TUI; headless pty attach fails (boots, 0 tokens, no command execution). §6 holds: cli-only default, nothing deletes on unproven surfaces. |
| 9 | Fenced delete e2e | ✅ | Post-consent closed chats deleted via the fence: **0 rows, 0 messages, 0 FTS hits, 0 files** (`sessions\|0`, `messages\|0`, no dump files, WAL `busy=0`); `last_sweep: deleted=1`; `status` shows `Sessions: deleted=4, excluded=1`. |
| 10 | TUI close | ⏸️ + made safe | GUI-only surface (not headless-driveable); same removal mechanism as rows 7/9 (unit + row-7/9 e2e). Per §6 `apply_to_platforms` now defaults to `["cli"]` — tui/desktop cleanup strictly opt-in; pinned by tests (`test_platform_scope_for_cleanup`, schema≡DEFAULTS). |
| 11 | Automatic reasons leave rows | ✅/🧪 | Unit: `ws_disconnect`-closed row survives; e2e row 20 (open row survives kill) proves the closed-only rule live. |
| 12 | Blocked-route aux disclosure | ✅ | Live observation during row 4: aux title call attempted against the blocked route independently (401) — the banner's "Background calls may already have run" is literally true; `status` line 2 discloses non-enforcement. |
| 13 | Pre-setup chats survive | ✅ | Pre-consent chat closed and survived all later sweeps (`row16 survives: 1`); ledger shows `excluded: cleanup not enrolled` for 13 pre-consent sessions. |
| 14 | Delegate subagents | 🧪 | Unit: delegate child (model_config `_delegate_from`) with no ledger entry refuses the whole fenced delete; parents/children verified untouched in the real-schema DB. |
| 15 | `/private keep` | ✅ e2e (core) | Real-session e2e: `chat -q` turn → keep handler (slash command's own write, in-process) → real `cli_close` → next-activity sweep → session SURVIVED: row present, 2 messages intact, ledger `excluded`+`keep`. Confirmation: non-kept closed sessions force `chat -q` id rotation; the kept one stayed resumable. Delegate-child fence-refusal half unit-pinned. |
| 16 | No consent → nothing ever deleted | ✅ | Every pre-consent session in the live harness ledger is `excluded: cleanup not enrolled`; sweeps ran (`last_sweep` recorded) and deleted 0 of them. |
| 17 | Neighbour-safety (abc/abc_extra) | ✅/🧪 | Contract: dump-name timestamp regex cannot match neighbour dumps (exact-name corpus test); unit: neighbour rows never touched; e2e: 29 unrelated session files intact post-sweep. |
| 18 | Profile switches | 🧪 | Unit: `_plugin_home_key`-style per-home state (store paths derive from captured home); generation requirement excludes sessions born pre-switch. |
| 19 | Log redaction | ✅ non-vacuous | Sentinel message flowed (assistant reply returned; db row existed pre-sweep) yet **0 occurrences** across agent.log, gateway.log, tool_calls.log; `[redacted]` visible on the inbound/turn lines in agent.log. |
| 20 | Crash mid-session | ✅ | Gateway `kill -9` with a turn in flight → row stays **open with `<null>` end_reason**, survives the next sweep; no invented close. |
| 21 | Status coverage | ✅ | `hermes incognito-private-mode status` renders mode, 3 coverage lines (⛔/⚠️/machine), consent state, arming generation, session counts, last sweep, deep-clean reminder, "What stays", command list. |
| 22 | Live GLM pass-through + labels | ⚠️ label + synthetic block LIVE; GLM turn ⏳ pending | Live: status line 1 = "⚠️ unverified retention; allowed with a warning" on the zai route, and the ⛔ synthetic block on open.bigmodel.cn (row 4). An earlier 2026-11-10 note recorded a GLM "pong" reply, but the final diagnosis (SUBMISSION §3, 21:15 UTC) is that every available harness key returns HTTP 429 code 1113 (no GLM plan entitlement) on both z.ai endpoints, so the live GLM turn is counted as **pending** until a plan-entitled key is available. The spec's openai-codex half of this row is covered by the Codex live-matrix row. |
| 23 | Live Nous + zdr flags on wire | ✅ LIVE 2026-11-10 | nous pass-through (`z-ai/glm-5.3-flash` via inference-api.nousresearch.com) completed. The provider explicitly evaluates the request's data-use policy — a model without a no-training endpoint returns 403 "no endpoint … meets this request's data-use requirement (no_training, zero_retention)" — proving the privacy policy reaches and is enforced provider-side; the plugin's error translation rendered it in plain English. Memory veto observed LIVE with a working model: tool call attempted → vetoed → model reported honestly; MEMORY.md md5 unchanged, canary absent. |
| 24 | P1 transform routes | 📋 documented | route-table rows exist (`p1_unimplemented` kind) but transforms are not implemented in P0; classification never blesses them (label `warn` ceiling) — unit-tested. |

## Audit round addendum (Opus review, 2026-11-10 19:08 UTC)

Three independent Opus reviews (privacy/correctness, security/vuln, independent
re-verification) ran against the build. Verdicts: critical-issues / needs-fixes /
mostly-verified. All HIGH findings were fixed the same evening and pinned by new
regression tests (`tests/unit/test_audit_fixes.py`); the suite was **96
passed** after that round (127 at c954724). Key fixes: blank-line memory-entry leak (exact-reconstruction strip +
fail-closed ambiguity), mode-off chats excluded forever, compaction lineage
marked only for confirmed-removed targets (+`deleting` intent so the fence is
no longer mislabeled "external"), unledgered-predecessor protection, logfilter
ReDoS (unambiguous regex + 4 KB cap + tool_calls preview redaction), fail-closed
llm_execution, provider-id spoofing, loopback forwarding-unknown label, absent
vs error row reads, URI percent-encoding, `/private on` override, raw-id
dropping, sweep-attempted accounting.

Honest notes the audit raised that are NOT silently dropped:
- COVERAGE row numbers differed from BUILD_SPEC §6 (rows 12/14/18/24 were
  mislabeled or untested). The underlying failures (compaction lineage,
  mode-off generations) are FIXED and regression-tested; the interactive rows
  (8/10) are now skipped-with-reason AND made safe by the §6 opt-in default
  (`apply_to_platforms: [cli]`); row 15's core keep-survival property is e2e
  verified over real sessions.
- Ledger `deleted_how` was always "external" before the fix (our own fence
  fired on_session_delete); now "fenced" during intent-guarded deletes.
- `last_sweep.attempted` under-reported (always 0); fixed.

## Post-audit feature round (2026-11-11)

- **Usability presets** (`preset complete` / `preset provider-private` + wizard
  picker + status detection): writer-only — sets explicit, inspectable values;
  never grants cleanup consent. Pinned by 8 tests (shape, roundtrip, CLI apply,
  consent-untouched, status line).
  Correction: `tests/unit/test_presets.py` holds **7** preset tests; the
  preset CLI real-parser test is one of the 8 tests in
  `tests/unit/test_adversarial_round.py`.
- **Plaintext-remote block**: `allow_plaintext_remote` default false — http://
  to non-loopback hosts blocked with plain-English reason. 3 tests.
- **Persona strip** (`strip_persona`, default false): best-effort exact-span
  SOUL.md removal with honest "present-not-located" disclosure in the audit
  trail. 3 tests. Known bound: if core post-processing alters the text, the
  strip cannot match — disclosed, never falsely claimed.
- **Outbound audit log**: `audit.jsonl` (0600, capped 512KB→newest half),
  content-free metadata only (endpoint/verdict/actions — asserted by test that
  the user content string is absent); `/private status` summarises last hour.
- **DeepSeek LIVE row (extra)**: `deepseek-chat` turn completed (exit 0);
  memory veto observed live (blocked message, MEMORY.md md5 unchanged, canary
  absent); ⚠️ unverified label correct. Second real provider with the full
  chain proven, alongside Nous.

Suite at that round: **113 passed** (three consecutive runs, pollution-proof; now 127) · validate green · commits eaf51f7/201d9b0/6991ef5.

## Live-provider matrix (2026-11-11)

| Provider | Route class | Pass-through | Memory veto intact | Label correct |
|---|---|---|---|---|
| Nous Portal | ok-if-flagged (flags provider-enforced) | ✅ live | ✅ live | ✅ live |
| DeepSeek | UNVERIFIED (warn) | ✅ live | ✅ live | ✅ live |
| OpenRouter | ok-if-flagged (PER_REQUEST flags injected) | ✅ live | ✅ live (MEMORY.md unchanged, no canary) | pinned |
| OpenAI Codex | UNVERIFIED → ⚠️ warn (`openai-codex-backend`; blocked under both presets) | ✅ live (allowed-with-warning) | ✅ live (blocked message shown) | pinned |
| Xiaomi MiMo | NONE_RETENTION → ⚠️ warn | ✅ live | ✅ live | ✅ live |
| zai/GLM | ⚠️ label + synthetic block live (row 4); turn pending plan-entitled key | ⏳ account-side | ✅ unit + label live | ✅ live |
| MiniMax | UNVERIFIED (warn) | ⏳ 402 insufficient balance (account-side, translated plainly) | ✅ MEMORY.md unchanged in the 402 run | ✅ live |

Live pass-through rows for ⚠️ routes (DeepSeek, Codex, MiMo) ran with
`allow_unverified: true` (the default); both presets set it to false, which
blocks those routes.

Five providers with complete live chains; the two pending ones fail on
owner-account entitlement (402/429), with the plugin's error translation and
labels behaving correctly in those runs. Base_url lesson: OpenRouter must be
configured WITHOUT an explicit base_url (the runtime self-resolves; an explicit
canonical base_url drops the credential pool — `runtime_provider.py`
`_openrouter_should_use_pool`).

## Checkpoint-1 metric (rows 1–3, 6, 7–9, 13, 16, 17)

**7 of 9 fully green with e2e evidence; row 6 unit-green (live veto pending owner
re-login); row 8 source-pinned (interactive path pending).** Nothing silently
skipped: every gap above names its blocker. (Checkpoint-1 snapshot: row 6's
`memory` veto has since been observed live, see row 6.)

## Release gaps (against BUILD_SPEC §6)

- **Spec row 12 (compressed chat: whole lineage gone) fails by design.**
  Compression parents end with `end_reason='compression'`, which is not in the
  plugin's user-close allowlist, so they never count as closed and are never
  deleted. The plugin keeps them and discloses it (close notice "N earlier
  part(s) of this chat remain", README *What stays*). The disclosure is pinned by
  `test_finalize_discloses_remaining_lineage`; no e2e row.
- **Spec row 14 (generations: enable→disable→create→enable→resume→close →
  survives)** has no evidence row: unit-tested only
  (`test_mode_off_chat_survives_reenable`, `test_arming_needs_generation_span`).
- **Spec row 18 (two isolated profiles, overlapping ids)** has unit/contract
  evidence only (row 18 above); no two-profile e2e run.
- Spec row 24 (messaging fake platform) and row 27 (Desktop badge) have no
  evidence. Rows 8 and 10 remain skipped-with-reason (cleanup is CLI-only by
  default). Row 22's live GLM turn is pending (account entitlement).
