# incognito-private-mode

A Hermes plugin that gives you honest, verifiable privacy controls: provider
privacy flags, memory stripping, tool vetoes, and consent-gated history
removal — with a status command that tells you exactly what is and is not
covered. No blanket "Incognito mode" theater; coverage lines, never vibes.

Built per `BUILD_SPEC.md` v3 (canonical). Local git only.

## Install

```bash
hermes plugins install <source-url-or-path>   # or drop this directory into ~/.hermes/plugins/
hermes plugins enable incognito-private-mode
hermes incognito-private-mode setup            # guided setup (TTY only), records cleanup consent
```

Requires Hermes >= 0.22.5. Python: whatever Hermes runs (3.11/3.14 tested).
No extra dependencies — Hermes-provided SDK types only.

## What it does

| Coverage line | What happens | Enforcement |
|---|---|---|
| 1. Main chat requests | Curated route table classifies the endpoint host (authoritative) + provider id. ⛔ training routes are blocked outright; ⚠️ unverified routes are allowed with a warning by default; ok-if-flagged routes get `extra_body.provider.data_collection=deny, zdr=true` injected copy-on-write. | enforced |
| 2. Background calls (titles, compression, vision) | Classification only — aux calls are **not** intercepted in P0; `status` says so explicitly. | disclosed, not enforced |
| 3. On this machine | Consent-gated history removal: sessions are hashed (SHA-256, `s:`-prefixed) into a provenance ledger; after you close a chat, an opportunistic sweep deletes exactly that chat — messages, FTS, dump files — through Hermes' own fenced public API. | enforced after setup consent |

Plus:

- **Memory strip** — built-in memory blocks (`MEMORY (your personal notes)` /
  `USER PROFILE (who the user is)`) and `<memory-context>…</memory-context>`
  spans are removed from outgoing requests while private mode is on
  (copy-on-write; the caller's objects are never mutated).
- **Tool vetoes** — `memory` write, `skill_manage`, `session_search`, and
  external memory-provider tools fail closed with a clear message when the
  corresponding setting is off.
- **Log redaction** — while private mode is on, inbound-message and
  conversation-turn log lines carry `[redacted]` instead of your text.
- **API error translation** — provider "no private endpoint matching data
  policy" errors become one plain-English sentence instead of a retry loop.
- **`/private` chat command** — `status · keep · on · off · cleanup · deep-clean`.

## Status

```
$ hermes incognito-private-mode status
Private Mode: ON — coverage, never a blanket Incognito:
1. Main chat requests: ⛔ route trains with no opt-out — requests are blocked
2. Background calls (titles, compression, vision): not enforced — ⚠️ unknown (auto routing)
3. On this machine: history removed on close; some copies remain
...
What stays (never removed by this plugin): final replies kept for retries (by Hermes design),
CLI input history, paste files, interrupt_debug.log, deleted bytes in free DB pages until a
deep clean, auxiliary calls without our flags, max-iterations summary call, ...
```

## Safety design (the short version)

- **Fail closed on deletes, fail open on requests.** Deletion only proceeds
  for entries the ledger can fully prove (armed lineage + closed with an
  explicit user end-reason, all targets armed). Any unprovable target refuses
  the whole operation. Request middlewares never raise into your turn.
- **Hermes' own fence is the only delete path.** `SessionDB.delete_session(
  sessions_dir=None, expected_delete_ids=…, exclude_active_write_guards=True)`.
  Never the CLI delete, never `--force`, never compression-chain deletion.
- **No subprocesses anywhere.** Everything is the in-process public API.
- **Open rows are sacred.** Sessions that never closed, or that closed via an
  automatic reason (`ws_disconnect`, idle timeout, …), are never deleted.
- **Pre-setup chats survive forever.** Cleanup applies only to sessions born
  after you recorded consent ("arming generation"), and only after a
  24-hour generation span — so a mistaken setup can never eat old history.
- **Settings/consent never leak to the model.** The ledger, confirmations and
  consent live plugin-side (0600, flock-serialized); prompts only ever see
  hashed ids and verdict labels/counts.
- **`keep` is honored forever** — an excluded chat stays excluded, across
  generations and profile switches.

## Settings (Desktop/TUI Plugins hub or `plugins.entries…settings`)

`mode` (`private`|`off`), `apply_to_platforms` (cleanup scope), `keep_history`,
`use_memory`, `save_memory`, `learn_skills`, `allow_unverified`,
`block_training`, `deep_clean_reminder`. Per-chat overrides via `/private
on|off|keep` (only ever widen protection in mode `off`).

## Honest limitations

- Background/auxiliary calls are classified and disclosed, not intercepted
  (`status` line 2).
- Blocked-route banner is per api_mode: chat_completions/anthropic_messages/
  codex_responses get a synthetic reply; other modes fail the turn with the
  provider error (disclosed).
- Deleted bytes remain in free SQLite pages until a manual deep clean
  (`status deep-clean` prints the exact steps); SSD remanence is physics.
- The codex_app_server keeps its own runtime state; save_trajectories and
  gateway shutdown spools are out of reach by design (see `status`).
- Interactive TUI surfaces were exercised indirectly (one-shot CLI paths were
  the e2e workhorse); the TUI uses the same hooks and finalize path.

## Development

```bash
source ../harness/env.sh          # isolated HOME + expired-credential-free harness
python -m pytest tests/           # 80 unit+contract tests
hermes plugins validate <dir>     # catalog validation
```

License: MIT.
