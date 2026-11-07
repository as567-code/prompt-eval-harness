# CLI contract notes (captured live 2026-11-06)

Source: `scripts/capture_cli_fixtures.py` (run with `PEH_LIVE=1`), fixtures in `tests/fixtures/cli/`.
Every call went through `run_cli` with the allowlisted `safe_env` env. OAuth worked under that env
with no extra variables (R10 not triggered). No `--bare`, no `--fallback-model`.

## Versions

- `claude --version`: `2.1.319 (Claude Code)`
- `codex --version`: `codex-cli 0.167.0`

## Subscription-auth signals (R9)

| CLI | Where | Exact field or line | Value observed |
|---|---|---|---|
| Claude | First line of `--output-format stream-json --verbose` (`type=system`, `subtype=init`) | `apiKeySource` | `"none"` (no API key in use; OAuth subscription) |
| Codex | `codex login status` stdout (exit 0) | whole line | `Logged in using ChatGPT` |

Guard rule: Claude is subscription auth iff the init event has `apiKeySource == "none"` (any other
value, e.g. an env or config key source, means API-key billing, so refuse). Codex is ChatGPT auth iff
the status output contains `Logged in using ChatGPT` (an API-key login prints a different line).
The non-stream `json` result has no auth field, so the check needs its own stream-json call.
The Claude `result` event also carries `total_cost_usd`/`costUSD`, but these are list-price
notional values, not billed spend. The init event also had `plugins` (4 builtin), `skills` and
`slash_commands` lists, but `tools` and `mcp_servers` were empty as intended.

## Claude

Flags used (verbatim):

```
claude -p "<prompt>" --model claude-haiku-5-5 --effort low --tools "" --strict-mcp-config
  --setting-sources "" --system-prompt "<s>" --output-format json --no-session-persistence
  [--json-schema '<schema>']
```

For the stream capture, `--output-format stream-json --verbose` replaces `--output-format json`.

- Output: one JSON object (`type: "result"`). Fields used: `result`, `is_error`, `stop_reason`,
  `terminal_reason`, `num_turns`, `permission_denials`, `duration_api_ms`, `total_cost_usd`,
  `usage` (`input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`,
  `cache_creation.ephemeral_1h_input_tokens`, `output_tokens_details.thinking_tokens`, `iterations[]`),
  and `modelUsage` keyed by model id (`claude-haiku-5-5`; `inputTokens`, `outputTokens`, ...).
- **Structured output** (`--json-schema`): the parsed object appears under a top-level
  `structured_output` key (`{"answer": "ok"}`). `result` also holds the same JSON as a string.
  Note `num_turns` is **2** and `stop_reason` is `"tool_use"` for a schema call (the CLI
  uses a synthetic structured-output tool), while `permission_denials` stays empty and
  `terminal_reason` is `"completed"`. The tool audit rule "`num_turns` must be 1" would
  wrongly flag schema calls; the provider must allow `num_turns == 2` when `structured_output` is set.
- **Tool bait** (`cat /etc/hostname` with `--tools ""`): `result: "CANNOT"`, `num_turns: 1`,
  `permission_denials: []`. No tool was available, so nothing was attempted.
- **Output-token cap**: `extra_env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64"}` **works**, but it is a
  hard error, not a graceful truncation. The process exited **1** with `is_error: true`,
  `terminal_reason: "api_error"`, `api_error: "max_output_tokens"`, `stop_reason: "stop_sequence"`,
  `num_turns: 4` (the CLI retried), `usage.output_tokens: 256` total (`usage.iterations[0].output_tokens: 64`),
  and `result` set to `"API Error: Claude's response exceeded the 64 output token maximum. ..."`.
  No partial text is returned. The cap is therefore applied (`output_cap_applied=True`), and an
  over-cap response must be detected via `is_error` + `api_error == "max_output_tokens"` rather than
  treated as a normal answer. Fixture: `claude/cap_haiku.json`.
- Refusal and usage-limit errors were not observed. The stream-json `rate_limit_event` shows
  `rate_limit_info.status: "allowed"`, `rateLimitType`, `unifiedWindows.five_hour|seven_day.utilization`
  (useful for usage-limit detection).

## Codex

Flags used (verbatim, plus `-C <cwd> "<prompt>"` at the end):

```
codex exec --json --ignore-user-config --ignore-rules --skip-git-repo-check -s read-only
  -m gpt-6.1-terra -c 'model_reasoning_effort="low"' -c 'model_instructions_file="<file>"'
  -c 'web_search="disabled"' --disable apps --disable browser_use --disable browser_use_external
  --disable computer_use --disable image_generation --disable multi_agent --disable memories
  --disable plugins --disable skill_search --disable sleep_tool --disable shell_tool
  --disable unified_exec --disable view_image --disable goals --disable tool_suggest
  --disable hooks --disable in_app_browser --disable realtime_conversation
  --disable code_mode_host [--output-schema <file>] [-c model_max_output_tokens=64]
```

- `--json` stdout is JSONL: `thread.started` (`thread_id`), optional `item.completed` events with
  `item.type == "error"` (a warning, see below), `turn.started`, `item.completed` with
  `item.type == "agent_message"` (`item.text`), `turn.completed` with
  `usage` (`input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`,
  `reasoning_output_tokens`). Every run emitted an `error` item "Code Mode is unavailable because
  code-mode host is disabled..." before `turn.started`; parsers must ignore `error` items and not
  treat them as failures (exit code was 0).
- **Structured output** (`--output-schema <file>`): JSON as a string in the `agent_message` text
  (`"{\"answer\":\"ok\"}"`). There is no separate structured field.
- **Tool bait**: the stdout stream shows only `agent_message: "CANNOT"`. The rollout shows the model
  tried one `custom_tool_call` (`name: "exec"`, an `ALL_TOOLS.filter(...)` snippet), and the matching
  `custom_tool_call_output` was `"code-mode host is disabled"` (the fail-closed message the PRD allows).
  So the audit must read the rollout, and this call is allowed, not `tool_use_detected`.
- **Rollout** (`rollouts/<fixture>.jsonl`): first line `type: "session_meta"`, `payload` has `id`
  (= thread id), `session_id`, `cwd`, `cli_version`, `originator`, `source` (and, unsanitized,
  `creator_user_id`/`creator_account_id`, dropped from fixtures). `type: "turn_context"` lines
  carry `payload.model` (`gpt-6.1-terra`) and `payload.effort` (`low`). Tool calls are
  `response_item` lines with `payload.type` `custom_tool_call` / `custom_tool_call_output`. The file lives under
  `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl`. Each rollout is about 50 KB because
  `session_meta.base_instructions` and tool descriptions are embedded.
- **Output-token cap**: `-c model_max_output_tokens=64` is **unsupported**. Codex prints
  `Codex is ignoring 1 unrecognized configuration setting ... session-flags: model_max_output_tokens is ignored.`
  (as `error` items in the JSON stream, exit 0), the model counted to 400, and `turn.completed.usage.output_tokens`
  was 1202. The provider must set `output_cap_applied=False` for Codex.
- Refusal and usage-limit errors were not observed.

## `codex delete <thread_id>`

- Plain `codex delete <id>` is **not** non-interactive: it exits 1 immediately (about 130 ms, no hang) with
  `Error: cannot confirm session deletion without an interactive terminal; rerun with --force and a session UUID`.
  The rollout file remained.
- `codex delete --force <thread_uuid>` works non-interactively: exit 0, prints
  `Deleted session <uuid>.`, and the rollout file disappears (verified for all 4 own sessions,
  each about 0.2 s). Use `--force` plus the UUID, and only after verifying the rollout's
  `session_meta.cwd` is under the provider's own work root. A fixture run left 4 own sessions that
  were then cleaned this way (identified by `session_meta.cwd` containing the capture temp dir, never by mtime).

## Sanitizing

`scripts/capture_cli_fixtures.py` replaces `str(Path.home())` with `/HOME` and the OS username with `USER`,
drops every JSON key matching `(?i)(credential|user_id|account_id|email)` at any depth (for example
Claude's `credential_expires_at`), replaces `session_id`/`uuid`/`thread_id` values with fixed placeholders
(`01aaaaaa-0000-7000-8000-00000000000N` for Codex threads, shared by stdout and rollout; `01bbbbbb-...` for
Claude ids), and replaces the capture temp dir with `/TMP/cwd`. Other opaque ids (turn ids, message ids,
request ids) are left as captured; they are not identifying. Fixtures stay valid JSON/JSONL.
`test_fixtures_present` asserts `/Users/`, `credential`, `account_id`, `user_id`, email-like strings
and the username are absent.
