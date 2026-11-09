# BUILD_LOG — incognito-private-mode (BUILD_SPEC v3)

Builder: GLM (zai) via Hermes; owner Tranquil-Flow. Spec: BUILD_SPEC.md v3 (canonical).
Log format per slice: built, exact commands + results, source mismatches, risks, acceptance table, progress bar.

## Justified core imports (spec §2.1)

- `hermes_state` (SessionDB: get_session_delete_targets / delete_session fenced recipe) — public allowlist.
- `hermes_constants.get_hermes_home`, `hermes_cli.config.load_config_readonly`, `hermes_cli.plugins_state.PluginState`, `agent.memory_provider.spawn_context_thread` — public allowlist.
- `hermes_cli.plugins_state.save_plugin_setting` — NOT on the allowlist by name; the module is (PluginState). It is the one documented writer for `plugins.entries.<id>.settings` ("THE writer for plugin settings" — used by ctx.set_config and the Desktop hub itself). Used only by the TTY setup wizard. Justified here per spec §2.1; flagged by validate as a warning, never a failure.

## OpenAI SDK types

synthetic.py imports openai.types.* — Hermes-provided SDK types are allowed (§2.1 "no extra dependencies (Hermes-provided openai SDK types allowed)"). No python_dependencies declared.

## Source mismatches found while building

1. **PluginState data namespace** — `_plugin_data_namespace()` resolves plugin data to
   `plugin-data/agent-plugin-<id>-<hash8>`, NOT `plugin-data/<id>`. Early e2e probes that
   bypassed the plugin's own path resolution wrote to a fallback dir and made correct
   no-op behavior look like a bug. All in-repo code uses `capture_paths()` (PluginState).
   Test-side: `tests/conftest.py` appends the repo root to `sys.path` (GOAL forbids
   PYTHONPATH; core modules like `hermes_yaml`/`registration_lifecycle` live at the root).
2. **`chat -q` delegates to an auto-spawned gateway** (`gateway run --quiet --idle-exit`,
   pm-runtime Python 3.14) — plugin code changes require letting the idle gateway exit
   (or killing the exact PID after verifying its HERMES_HOME). Two stale-code incidents
   traced to this.
3. **Memory block format** — real `_render_block` output is
   `[3% — 71/2,200 chars]` (em-dash, thousands separator, "chars"); the research-phase
   sample `[13%· 2197/2200]` differs. `MEMORY_TITLE_RE` accepts both (`\[\d+%[^]]*\]`).
4. **`register_cli_command(setup_fn)`** — setup_fn receives THIS plugin's argparse parser
   (main.py builds it named after the plugin); subcommands attach directly, no extra
   add_parser level.
5. **Session rotation** — the gateway rotates local chat sessions per turn; "the" session
   for a one-shot query is a NEW row each time. Row-9 style assertions must target the
   rotated ids, not the first row seen.
6. **New core end reason** — `adopted_by_profile` (profile adoption re-homes rows) is not
   a user close; added to the reviewed ignore list in the contract scan.
7. **Middleware `next_call` contract** — downstream execution is single-use; a middleware
   must never call `next_call` twice (its retries would re-run). Final structure: all
   plugin logic guarded, `next_call` invoked exactly once outside the guard so downstream
   provider errors propagate untouched.
8. **`save_plugin_setting` signature** — `(plugin_id, (path…), value)` tuple-path form.

## Bugs found and fixed during e2e

- sweep lost-update: whole-file snapshot write clobbered concurrent sweeps → merge
  only entries changed this sweep, applied under the lock (60→61 tests caught variants).
- entries armed in the same sweep pass were never considered for deletion → same-sweep
  delete now works (live-verified).
- strip block span over-ran into following sections when no trailing rule existed →
  span bounded by next rule / first blank line after entries.
- dump-file counter counted non-existent names → only real removals counted.
- log filter: unquoted f-string `msg=` variants now redacted too.

## Acceptance table

See COVERAGE.md (row-by-row statuses + evidence). Summary at STOP (2026-11-10):

- ✅ e2e-verified: rows 1, 4, 5 (non-vacuous control), 7, 9, 13, 16, 17, 19, 20, 21
- 🧪 unit/contract-verified: rows 2, 3, 6, 8, 10, 11, 12*, 14, 15, 18, 24
- ⏸️ blocked on owner: rows 22, 23 (harness OAuth expired ~21:19 UTC Nov 9; 1 h TTL,
  no static keys; owner re-login required — never a builder action)

Progress: ████████████████░░░░ 80% (tests 80/80; validate green; docs done; SUBMISSION.md next)
