# incognito-private-mode

**Private Mode, honestly.** A Hermes plugin that adds privacy controls you can
check: provider privacy flags, memory stripping, tool vetoes, and
consent-gated history removal, plus a status command that tells you exactly
what is and is not covered. No blanket "Incognito mode" claim; every
protection is a coverage line with a stated scope.

## Pick your profile (30 seconds)

```bash
hermes plugins enable incognito-private-mode
hermes incognito-private-mode preset complete          # or: provider-private
hermes incognito-private-mode setup                    # optional: history removal (asks consent)
```

Both presets write the same provider-side settings; they differ in what
Hermes keeps on your machine. Each cell states what the plugin enforces and
where that stops.

| | `preset complete` | `preset provider-private` |
|---|---|---|
| **Main-chat requests** (enforced by middleware) | only routes rated ok, ok-if-flagged (flags injected and checked) or an account setting you confirmed in setup; ⛔ training, ⚠️ unverified, unknown and local/loopback endpoints are blocked | same |
| **Background calls** (titles, compression, vision, summaries) | classified and disclosed, **not enforced**: no flags, strip or blocks are applied to them | same |
| **Memory blocks in main-chat requests** | stripped (request blocked if removal cannot be proven) | stripped (same) |
| **Persona (SOUL.md) in main-chat requests** | stripped, best-effort (status/audit say when it could not be located) | same |
| **Model can save memories / learn skills** | no: `memory`, `skill_manage` vetoed | yes: saved locally by Hermes; memory blocks are still stripped from later main-chat requests |
| **Past-chat search (`session_search`)** | vetoed | allowed: results from past chats go into the conversation and to the provider |
| **Hermes chat history** | removed on close for **CLI chats (default scope)**, only after setup consent, only for chats started after consent; earlier compressed parts are kept (see *What stays*) | kept |
| **Plaintext http:// to remote hosts** | blocked | blocked |
| **Outbound audit trail** | local, content-free | local, content-free |

Strict blocking means a local model (Ollama, LM Studio, a LiteLLM proxy) is
blocked under both presets: a loopback endpoint may forward anywhere, so it is
rated ⚠️. Set `allow_unverified: true` to use one (it is then allowed with a
warning). Neither preset touches what external memory providers receive (see
*What stays*).

Everything is still an individual setting; presets just write the bundle.
Tweak any setting afterwards (see *Settings* below); `/private status` tells you
which profile you're running (`complete`, `provider-private`, or `custom`).

## Install

```bash
hermes plugins install <source-url-or-path>   # or drop this directory into ~/.hermes/plugins/
hermes plugins enable incognito-private-mode
hermes incognito-private-mode setup           # guided wizard (TTY only), records cleanup consent
```

Requires Hermes >= 0.22.5. Python: whatever Hermes runs (3.11/3.14 tested).
No extra dependencies; Hermes-provided SDK types only. AGPL-3.0.

## What it does

| Coverage line | What happens | Enforcement |
|---|---|---|
| 1. Main chat requests | Curated route table classifies the endpoint **host** (authoritative) + provider id. ⛔ training routes blocked outright; ⚠️ unverified/unknown routes allowed-with-warning by default (strict mode, used by both presets, blocks them); ok-if-flagged routes get `extra_body.provider.data_collection=deny, zdr=true` injected copy-on-write. | enforced |
| 2. Background calls (titles, compression, vision) | Classification and counting only. Aux calls are **not** intercepted; `status` says so explicitly. | disclosed, not enforced |
| 3. On this machine | Consent-gated history removal: sessions are hashed (SHA-256, `s:`-prefixed) into a provenance ledger; after you close a chat on a surface in `apply_to_platforms` (default: CLI only), an opportunistic sweep deletes that chat's rows, messages, FTS entries and dump files through Hermes' own fenced public API. Compression parents are not deleted (see *What stays*). | enforced after setup consent; unproven CLI close paths (/new, resume switch, branch) never auto-delete — only quitting the CLI is proven |

Plus:

- **Memory strip** (when `use_memory` is false): built-in memory blocks
  (`MEMORY (your personal notes)` / `USER PROFILE (who the user is)`) and
  `<memory-context>…</memory-context>` spans are removed from outgoing
  **main-chat** requests (copy-on-write; exact text reconstruction, so
  multi-paragraph entries, even ones containing the `§` separator, are
  removed whole; when exact reconstruction is impossible the request is
  **blocked**, not sent). Background calls are not stripped.
- **Tool vetoes**: `memory` (unless `save_memory`), `skill_manage` (unless
  `learn_skills`), `session_search` (unless `keep_history`), and an external
  memory provider's tool **only when it is named after the provider id**.
  A veto returns a clear message; an internal error in the check also vetoes — SDK-shaped responses for the three known transports, and for any other transport an allowlist-scrubbed request (only the model field survives; the turn fails downstream with zero user content sent). Residual: a third-party middleware downstream of this plugin could reconstruct content from the middleware chain's original_request context — unsupported transports are therefore best-effort protected and this is disclosed rather than claimed as absolute.
- **Log redaction**: inbound-message, conversation-turn and tool-call preview
  log lines carry `[redacted]` instead of your text while mode is not `off`;
  the redactor is linear-time (no ReDoS). Lines written before the plugin
  loaded are not touched.
- **Plaintext-remote block**: main-chat `http://` to anything non-loopback is
  blocked (cleartext egress) unless you set `allow_plaintext_remote`.
- **Persona strip (opt-in `strip_persona`, on in both presets)**: your SOUL.md
  text is located verbatim in main-chat requests and replaced; when it cannot
  be located, the audit record and status say so instead of claiming
  protection.
- **Outbound audit log**: a local, content-free record of every main-chat
  provider request while private mode is on (host, verdict, blocked, persona
  outcome) at
  `audit.jsonl`; `/private status` summarises the last hour. Never message
  content, 0600, size-capped.
- **API error translation**: provider "no endpoint matching data policy" /
  billing errors become one plain-English sentence instead of a retry loop.
- **`/private` chat command**: `status · keep · on · off · cleanup · deep-clean`.

## Status tells the truth

```
$ hermes incognito-private-mode status
Private Mode: ON — coverage, never a blanket Incognito:
Profile: custom
1. Main chat requests: ⚠️ unverified retention; allowed with a warning
2. Background calls (titles, compression, vision): not enforced — ⚠️ unknown (auto routing)
3. On this machine: history cleanup not enabled (run `hermes incognito-private-mode setup`)
...
Outbound audit (last hour): 4 calls (ok-if-flagged×1, warn×3; 0 blocked); hosts: api.deepseek.com, ...
What stays (never removed by this plugin): final replies kept for retries (by Hermes design),
CLI input history (~/.hermes/.hermes_history), paste files, interrupt_debug.log, deleted bytes in
free DB pages until a deep clean, auxiliary calls without our flags, ...
```

(Illustrative output with default settings, which match neither preset.
Under either preset line 1 reads "unverified; blocked" for the same route.)

## What stays

The plugin never claims these are gone. `/private status` repeats the list.

| What stays | Why | How to remove it |
|---|---|---|
| Final replies of gateway-handled chats, kept for retries | Hermes design (core `state_meta` admission results) | Not removable by this plugin; it never deletes core receipt rows |
| CLI input history (`~/.hermes/.hermes_history`): typed and pasted text | Hermes' shared prompt history across all chats | Edit or delete the file yourself; the plugin never erases shared files |
| Paste files (`~/.hermes/pastes/`) | The CLI saves large pastes as files | Delete them yourself |
| `interrupt_debug.log` in your Hermes home | Hermes debug log | Delete it yourself |
| Deleted bytes in free SQLite pages | SQLite marks deleted pages free; it does not overwrite them | `/private deep-clean` prints the steps: quit Desktop/CLI, `hermes gateway stop`, `hermes sessions optimize` (never `--force`), restart. SSD remanence remains after that |
| Earlier parts of a compressed private chat (compression parents) | Hermes ends them with `end_reason='compression'`, which is not a user close, so the plugin never deletes them; the close notice reports how many remain | Delete them yourself with Hermes' own session tools if you want; the plugin never deletes compression chains |
| Background/auxiliary calls (titles, compression, vision, summaries) | Hermes sends them outside the main-chat middleware; the plugin only classifies and counts them, so no flags, strip or blocks apply | Point `auxiliary.<task>` at a route you trust; status line 2 shows its rating |
| Chats on TUI, Desktop and messaging surfaces | Cleanup scope defaults to `apply_to_platforms: [cli]` because those interactive rows are not e2e-verified; messaging cleanup is not built | Add `tui` / `desktop` to `apply_to_platforms` (opt-in); messaging: none |
| Chats started before consent, while mode was `off`, kept with `/private keep`, started with `keep_history: true`, or ended by an automatic reason or crash | By design: only consented, armed, user-closed chats are deleted | Delete them yourself with Hermes' own session tools |
| Everything an external memory provider received | A configured `memory.provider` sees every message; status warns when one is set | Remove the provider from config; its stored data is governed by that provider |
| Past chats returned by `session_search` (wire name `chat_history_lookup` on Anthropic OAuth) | With `keep_history: true` (provider-private) the tool is allowed, and its results are part of the conversation sent to the provider | Set `keep_history: false` to veto it |
| Max-iterations summary call, `codex_app_server` runtime, safe-mode sessions, gateway shutdown snapshots/spools, `save_trajectories`, `spawn-ledger.json` for `-q`, logs from before the plugin loaded, backups, terminal scrollback, messages on messaging platforms | Outside the plugin's hooks | Per item, manually; see `/private status` |

## Disclosures

- **In-process `state.db` writes**: deletion runs inside the Hermes process
  through Hermes' fenced public API (`delete_session(sessions_dir=None,
  expected_delete_ids=…, exclude_active_write_guards=True)`), followed by
  `PRAGMA wal_checkpoint(TRUNCATE)`. No CLI delete, no `--force`, no
  subprocesses.
- **Process-wide log filter**: attached in `register()` to the
  `gateway.run`, `agent.turn_context` and `hermes.tool_calls` loggers and
  their handlers; it redacts every record of those formats while mode is not
  `off` and is detached on unload.
- **Background cleanup thread**: sweeps run in a daemon thread
  (`ipm-sweep`) started from the session-start hook (rate-limited) and the
  session-finalize hook, at most 20 sessions per batch.
- **Local plugin state files** in the plugin data dir (0700): `ledger.json`,
  `confirmations.json`, `meta.json`, a lock file and `audit.jsonl`, all 0600.
  Session ids are stored hashed; the audit log holds no message content.

## Safety design (the short version)

- **Fail closed on deletes.** Deletion only proceeds for entries the ledger
  can fully prove (armed lineage + closed with an explicit user end-reason,
  fresh-ledger re-check under the lock). Any unprovable or unreadable target
  refuses the whole operation.
- **Requests: the rewrite step fails open, the final check fails closed.** If
  the request-rewrite middleware errors, the request continues to the final
  check; if the final check errors, the turn is blocked via the synthetic
  shape where the transport allows it.
- **Hermes' own fence is the only delete path**: never the CLI delete, never
  `--force`, never compression-chain deletion. No subprocesses anywhere.
- **Open rows are sacred**: sessions that never closed, or closed via an
  automatic reason (`ws_disconnect`, idle timeout, …), are never deleted.
- **Pre-setup chats survive**: cleanup applies only to sessions born after
  you recorded consent ("arming generation"); mode-off chats are excluded
  permanently, even after re-enabling.
- **`keep` is honored**: an excluded chat stays excluded across generations
  and profile switches; surviving lineage is never falsely marked deleted.
- **Settings/consent stay plugin-side**: the ledger, confirmations and consent
  live in 0600, flock-serialized files; the plugin's own messages show only
  hashed ids and verdict labels/counts.

## Settings

`mode` (`private`|`off`) · `apply_to_platforms` (cleanup scope, default `cli`)
· `remove_history_on_close` · `keep_history` · `use_memory` · `save_memory`
· `learn_skills` · `allow_unverified` (default `true`; `false` = strict) ·
`allow_plaintext_remote` (default `false`) · `strip_persona` (default
`false`) · `audit_log` (default `true`) · `block_training` ·
`deep_clean_reminder`. Per-chat overrides via `/private on|off|keep`: `off`
turns protections off for that chat; `on` and `keep` force private even when
global mode is `off`; `keep` also excludes the chat from cleanup.

## Honest limitations

- Background/auxiliary calls are classified and disclosed, not intercepted
  (`status` line 2).
- Blocked-route banner is per api_mode: chat_completions/anthropic_messages/
  codex_responses get a synthetic reply; other modes fail the turn with the
  provider error (disclosed).
- Deleted bytes remain in free SQLite pages until a manual deep clean
  (`deep-clean` prints the exact steps; it does not run them); SSD remanence
  is physics.
- Compressed private chats are only partly removed: compression parents stay
  (see *What stays*).
- Interactive TUI surfaces were exercised indirectly (one-shot CLI paths were
  the e2e workhorse); the TUI uses the same hooks and finalize path, and
  cleanup defaults to CLI-only until TUI/Desktop rows pass (opt in via
  `apply_to_platforms`).

## Development

```bash
source ../harness/env.sh                                   # isolated HOME harness
cd tests && env -u PYTHONPATH "$IPM_PY" -m pytest -q unit/ contract/   # 127 tests at c954724
hermes plugins validate <dir>                              # catalog validation
```

License: AGPL-3.0.
