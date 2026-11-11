# incognito-private-mode

**Private Mode, honestly.** A Hermes plugin that gives you real, verifiable
privacy controls: provider privacy flags, memory stripping, tool vetoes, and
consent-gated history removal — with a status command that tells you exactly
what is and is not covered. No blanket "Incognito mode" theater; coverage
lines, never vibes.

## Pick your profile (30 seconds)

```bash
hermes plugins enable incognito-private-mode
hermes incognito-private-mode preset complete          # or: provider-private
hermes incognito-private-mode setup                    # optional: history removal (asks consent)
```

| | `preset complete` | `preset provider-private` |
|---|---|---|
| **Provider sees** | only the conversation, only from verified no-training routes | only the conversation, flags injected, training routes blocked |
| **Memory blocks sent to provider** | never | never |
| **Persona (SOUL.md) sent to provider** | stripped (best-effort, disclosed) | stripped (best-effort, disclosed) |
| **Model can save memories / skills** | no | yes — stored locally by Hermes, never sent back out |
| **Hermes chat history** | removed on close (after consent) | kept — search/history fully work |
| **Unverified-retention providers** | blocked | blocked |
| **Plaintext http:// to remote hosts** | blocked | blocked |
| **Outbound audit trail** | local, content-free | local, content-free |

Everything is still an individual setting — presets just write the bundle.
Tweak any knob afterwards (see *Settings* below); `/private status` tells you
which profile you're running (`complete`, `provider-private`, or `custom`).

## Install

```bash
hermes plugins install <source-url-or-path>   # or drop this directory into ~/.hermes/plugins/
hermes plugins enable incognito-private-mode
hermes incognito-private-mode setup           # guided wizard (TTY only), records cleanup consent
```

Requires Hermes >= 0.22.5. Python: whatever Hermes runs (3.11/3.14 tested).
No extra dependencies — Hermes-provided SDK types only. AGPL-3.0.

## What it does

| Coverage line | What happens | Enforcement |
|---|---|---|
| 1. Main chat requests | Curated route table classifies the endpoint **host** (authoritative) + provider id. ⛔ training routes blocked outright; ⚠️ unverified routes allowed-with-warning by default (strict mode blocks them); ok-if-flagged routes get `extra_body.provider.data_collection=deny, zdr=true` injected copy-on-write. | enforced |
| 2. Background calls (titles, compression, vision) | Classification only — aux calls are **not** intercepted in P0; `status` says so explicitly. | disclosed, not enforced |
| 3. On this machine | Consent-gated history removal: sessions are hashed (SHA-256, `s:`-prefixed) into a provenance ledger; after you close a chat, an opportunistic sweep deletes exactly that chat — messages, FTS, dump files — through Hermes' own fenced public API. | enforced after setup consent |

Plus:

- **Memory strip** — built-in memory blocks (`MEMORY (your personal notes)` /
  `USER PROFILE (who the user is)`) and `<memory-context>…</memory-context>`
  spans are removed from outgoing requests while private mode is on
  (copy-on-write; exact text reconstruction, so multi-paragraph entries —
  even ones containing the `§` separator — are removed whole; when exact
  reconstruction is impossible the request is **blocked**, not sent).
- **Tool vetoes** — `memory` write, `skill_manage`, `session_search`, and
  external memory-provider tools fail closed with a clear message.
- **Log redaction** — inbound-message and conversation-turn log lines carry
  `[redacted]` instead of your text; the redactor is linear-time (no ReDoS).
- **Plaintext-remote block** — `http://` to anything non-loopback is blocked
  (cleartext egress) unless you explicitly allow it.
- **Persona strip (opt-in `strip_persona`)** — your SOUL.md identity text is
  located verbatim and replaced; when it cannot be located, status says so
  honestly instead of claiming protection.
- **Outbound audit log** — a local, content-free record of every provider
  request (endpoint, verdict, actions) at `audit.jsonl`; `/private status`
  summarises the last hour. Never message content, 0600, size-capped.
- **API error translation** — provider "no endpoint matching data policy" /
  billing errors become one plain-English sentence instead of a retry loop.
- **`/private` chat command** — `status · keep · on · off · cleanup · deep-clean`.

## Status tells the truth

```
$ hermes incognito-private-mode status
Private Mode: ON — coverage, never a blanket Incognito:
Profile: provider-private
1. Main chat requests: ⚠️ unverified retention; allowed with a warning
   (retention/training policy unverified for this provider — treat all chat content as retained)
...
Outbound audit (last hour): 4 calls (warn×3, ok-if-flagged×1; 0 blocked); hosts: api.deepseek.com, ...
What stays (never removed by this plugin): final replies kept for retries (by Hermes design),
CLI input history, paste files, interrupt_debug.log, deleted bytes in free DB pages until a
deep clean, auxiliary calls without our flags, ...
```

## Safety design (the short version)

- **Fail closed on deletes, fail open on requests.** Deletion only proceeds
  for entries the ledger can fully prove (armed lineage + closed with an
  explicit user end-reason, fresh-ledger re-check under the lock). Any
  unprovable or unreadable target refuses the whole operation. An internal
  check failure BLOCKS via the synthetic shape where the transport allows it.
- **Hermes' own fence is the only delete path** — never the CLI delete, never
  `--force`, never compression-chain deletion. No subprocesses anywhere.
- **Open rows are sacred** — sessions that never closed, or closed via an
  automatic reason (`ws_disconnect`, idle timeout, …), are never deleted.
- **Pre-setup chats survive forever** — cleanup applies only to sessions born
  after you recorded consent ("arming generation"); mode-off chats are
  excluded permanently, even after re-enabling.
- **`keep` is honored forever** — an excluded chat stays excluded, across
  generations and profile switches; surviving lineage is never falsely marked
  deleted.
- **Settings/consent never leak to the model** — the ledger, confirmations
  and consent live plugin-side (0600, flock-serialized); prompts only ever
  see hashed ids and verdict labels/counts.

## Settings

`mode` (`private`|`off`) · `apply_to_platforms` (cleanup scope, default `cli`)
· `remove_history_on_close` · `keep_history` · `use_memory` · `save_memory`
· `learn_skills` · `allow_unverified` (set `false` = strict) ·
`allow_plaintext_remote` (default `false`) · `strip_persona` (default
`false`) · `audit_log` (default `true`) · `block_training` ·
`deep_clean_reminder`. Per-chat overrides via `/private on|off|keep` (only
ever widen protection in mode `off`).

## Honest limitations

- Background/auxiliary calls are classified and disclosed, not intercepted
  (`status` line 2).
- Blocked-route banner is per api_mode: chat_completions/anthropic_messages/
  codex_responses get a synthetic reply; other modes fail the turn with the
  provider error (disclosed).
- Deleted bytes remain in free SQLite pages until a manual deep clean
  (`deep-clean` prints the exact steps); SSD remanence is physics.
- Interactive TUI surfaces were exercised indirectly (one-shot CLI paths were
  the e2e workhorse); the TUI uses the same hooks and finalize path, and
  cleanup defaults to CLI-only until TUI/Desktop rows pass (opt in via
  `apply_to_platforms`).

## Development

```bash
source ../harness/env.sh          # isolated HOME harness
python -m pytest tests/           # 113 unit+contract tests
hermes plugins validate <dir>     # catalog validation
```

License: AGPL-3.0.
