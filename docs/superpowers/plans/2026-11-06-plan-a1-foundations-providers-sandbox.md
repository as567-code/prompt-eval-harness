# Plan A1: Foundations, CLI Providers, Sandbox (P0–P2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a guarded Python repo, a $0 provider layer over `claude -p` and `codex exec` with exact usage accounting and tool-use auditing, and a hardened Docker sandbox that runs untrusted pytest suites.

**Architecture:**
- `src/promptharness/` is one package with focused modules: `usage`, `catalog`, `cost`, `security`, `providers/`, and `sandbox/`.
- Providers shell out to the vendor CLIs through one hardened subprocess helper and parse their JSON/JSONL into a common `Completion`.
- The sandbox pipes a tar into a locked-down container. Inside it, a trusted runner executes pytest as a child process and alone writes the result envelope.

**Tech Stack:** Python 3.12, uv, Typer, pydantic v2, PyYAML (`safe_load` only), defusedxml, pytest, ruff, mypy, bandit, pip-audit, gitleaks, zizmor, hadolint, Trivy, and Docker.

**Spec:** `docs/PRD.md`: §0, §5.5, §6.5, §8, §9, §11, and Appendix A. Phases and checklists are in `docs/ROADMAP.md` (P0, P1, P2). CLI facts verified on 2026-11-06 are in the Obsidian Run Log and repeated in Task 7.

## Global Constraints

- `requires-python = ">=3.12,<3.14"`. The package is `promptharness` with a `src/` layout. `uv.lock` is committed.
- No API keys and no SDK calls in default dependencies. Live model calls go **only** through the `claude` and `codex` binaries, and only when `PEH_LIVE=1`. CI never invokes either binary.
- Every subprocess call uses an argv list. Never use `shell=True`. stdin is always `subprocess.DEVNULL` for CLI calls. The child env is `security.safe_env()`, which drops every variable matching `(?i)(API_KEY|AUTH_TOKEN|SECRET|_TOKEN$|PASSWORD)`.
- YAML is loaded only via `yaml.safe_load`, and XML is parsed only via `defusedxml`. Host code never calls `eval`, `exec`, `pickle`, or `marshal` on model-derived data.
- Prices live only in `config/models.yaml`. Code never hardcodes a price.
- Never pass `--bare` or `--fallback-model` to `claude`.
- Codex rollouts are found by `thread_id`, never by mtime. A rollout is deleted only if its `session_meta.cwd` lies under the provider's own `work_root`.
- `mypy --strict src` and `ruff check` must be clean. Coverage must be ≥ 85% on `src/` (excluding `providers/sdk_*` stretch files).
- GitHub Actions: every `uses:` is pinned to a 40-character SHA, top-level `permissions: contents: read`, and no `pull_request_target`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Each phase lands as a PR labeled `ai-assisted`, with a body ending in `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

## Review Focus

1. **Non-JSON noise around CLI output.** Warnings like "Warning: no stdin data received…" or update banners can appear on stdout or stderr. Parsers must take the last JSON object (Claude) or only lines starting with `{` (Codex). Covered in Task 8 (`test_parse_tolerates_preamble`) and Task 10 (`test_parse_skips_non_json_lines`).
2. **Codex rollouts that cross midnight.** The rollout directory is the local date, so a call that starts at 23:59 lands in the next day's folder. `find_rollout` searches yesterday, today, and tomorrow. Covered in Task 9 (`test_find_rollout_across_midnight`).
3. **Docker daemon down or image missing.** This must surface as `SandboxStatus.SANDBOX_ERROR`, an infra error, never as a failed test. Covered in Task 14 (`test_daemon_down_is_sandbox_error`).
4. **Parallel sandbox runs.** Four concurrent runs must give four independent results, with unique container names and no shared temp state. Covered in Task 14 (`test_parallel_runs_are_isolated`).
5. **Invalid UTF-8 from a CLI or from pytest output.** Decode with `errors="replace"` and never raise. Covered in Task 6 (`test_invalid_utf8_output_is_replaced`) and Task 14 (`test_binary_garbage_stdout`).

---

## Phase P0: Foundations and guardrails

### Task 1: Project scaffold, CLI entry point, tooling config

**Files:**
- Create: `pyproject.toml`, `src/promptharness/__init__.py`, `src/promptharness/cli.py`, `.gitignore`, `.pre-commit-config.yaml`, `SECURITY.md`, `README.md`
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Produces: `promptharness.__version__ = "0.1.0"`, and a Typer `app` in `promptharness.cli` with console script `promptharness = "promptharness.cli:app"`. Later tasks add sub-apps with `app.add_typer(models_app, name="models")` and `app.add_typer(sandbox_app, name="sandbox")`.

- [ ] **Step 1: Init the project.** Run `uv init --package --name promptharness --python 3.12 --no-workspace .`, then `uv python pin 3.12`. Set `requires-python = ">=3.12,<3.14"`. Runtime deps: `typer`, `pydantic>=2`, `pyyaml`, `defusedxml`. Dev group: `pytest`, `pytest-cov`, `ruff`, `mypy`, `types-PyYAML`, `bandit`, `pip-audit`, `pre-commit`.
- [ ] **Step 2: Configure tools in `pyproject.toml`.**
  - ruff: `line-length = 100`, select `["E","F","W","I","B","S","UP","SIM","RUF"]`.
  - mypy: `strict = true`.
  - pytest: `testpaths = ["tests"]`, markers `live`, `docker`, `adversarial`, and `addopts = "-m 'not live'"`.
  - coverage: `fail_under = 85`, `omit = ["src/promptharness/providers/sdk_*"]`.
- [ ] **Step 3: Write `.gitignore`.** It covers `.env`, `.venv/`, `runs/`, `data/`, `site/`, `__pycache__/`, `.coverage`, `htmlcov/`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, and `.DS_Store`.
- [ ] **Step 4: Write the failing test.**

```python
from typer.testing import CliRunner
from promptharness import __version__
from promptharness.cli import app

def test_cli_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"promptharness {__version__}"
```

- [ ] **Step 5: Run it and confirm it fails.** Run `uv run pytest tests/unit/test_cli.py -v`. Expected: FAIL (ImportError, or no `--version` option).
- [ ] **Step 6: Implement.** Add a Typer `app` with an eager `--version` callback that prints `promptharness <version>`.
- [ ] **Step 7: Confirm it passes.** The same command now PASSes. `uv run ruff check . && uv run mypy --strict src` is clean.
- [ ] **Step 8: Add pre-commit hooks:** `ruff`, `ruff-format`, `gitleaks` (`repo: https://github.com/gitleaks/gitleaks`), and `bandit -c pyproject.toml -r src`, each pinned to a released tag. Write `SECURITY.md` covering scope (sandbox containment, $0 CLI-only execution), how to report a problem (open a private security advisory on the repo), and "no API keys are ever used". `README.md` is a one-paragraph stub linking `docs/PRD.md` and `docs/ROADMAP.md`.
- [ ] **Step 9: Commit** with `feat: scaffold promptharness package, CLI entry point, and tooling`.

### Task 2: Security utilities: safe YAML, IDs and paths, env allowlist, log redaction

**Files:**
- Create: `src/promptharness/security.py`
- Test: `tests/unit/test_security.py`

**Interfaces:**
- Produces:
  - `load_yaml(path: Path, *, max_bytes: int = 1_000_000) -> object`. It raises `ConfigError` on size or parse problems.
  - `validate_id(value: str, kind: str) -> str`, with pattern `^[a-z0-9][a-z0-9-]{2,63}$`. It raises `ValueError` naming `kind`.
  - `safe_join(root: Path, *parts: str) -> Path`. It resolves symlinks and raises `PathEscapeError(ValueError)` if the result is not inside `root.resolve()`.
  - `safe_env(extra: Mapping[str, str] | None = None) -> dict[str, str]`. It allowlists `PATH HOME USER LOGNAME SHELL TMPDIR LANG LC_ALL LC_CTYPE TERM __CF_USER_TEXT_ENCODING XDG_CONFIG_HOME CODEX_HOME CLAUDE_CONFIG_DIR`, then applies `extra`. A key matching `(?i)(API_KEY|AUTH_TOKEN|SECRET|_TOKEN$|PASSWORD)` is dropped, even from `extra`.
  - `RedactingFilter(logging.Filter)`, plus `install_redaction(logger: logging.Logger | None = None) -> None`.
  - `class ConfigError(Exception)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_load_yaml_rejects_python_tag`: a file containing `!!python/object/apply:os.system ["echo x"]` raises `ConfigError`.
  - `test_load_yaml_rejects_oversize`: a file of `max_bytes + 1` bytes raises `ConfigError`.
  - `test_validate_id_rejects_traversal`: for each of `["../x", "A", "a/b", "ab", "x"*65]`, `pytest.raises(ValueError)`.
  - `test_safe_join_blocks_escape`: `safe_join(tmp, "..", "etc")` raises `PathEscapeError`.
  - `test_safe_join_blocks_symlink_escape`: a symlink inside `tmp` pointing to `/etc` raises.
  - `test_safe_env_strips_keys`: with monkeypatched `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `GH_TOKEN`, and `HOME`, the result has `HOME` and none of the four. With `extra={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64", "MY_SECRET": "x"}`, the first key is kept and the second dropped.
  - `test_redaction`: logging `"key sk-ant-api03-AbC_123xyz and sk-proj-Zz9 and ghp_abcdefghijklmnopqrstuv and Bearer eyJabc"` emits a message containing none of the secrets, and containing `sk-***`, `ghp_***`, and `Bearer ***`.
- [ ] **Step 2: Run and confirm they fail.** Run `uv run pytest tests/unit/test_security.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement `security.py`** with the signatures above. The redaction regexes are `sk-[A-Za-z0-9_-]{3,}` → `sk-***`, `gh[pousr]_[A-Za-z0-9]{16,}` → `gh?_***` (keep the prefix letter), and `Bearer\s+\S+` → `Bearer ***`. Apply them to `record.msg` and to `record.args`, rendered as strings.
- [ ] **Step 4: Confirm they pass.** The same command now PASSes.
- [ ] **Step 5: Commit** with `feat: add safe YAML, ID/path, env, and log-redaction utilities`.

### Task 3: Usage, model catalog, notional cost

**Files:**
- Create: `src/promptharness/usage.py`, `src/promptharness/catalog.py`, `src/promptharness/cost.py`, `config/models.yaml`
- Test: `tests/unit/test_cost.py`, `tests/unit/test_catalog.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Usage`, with fields `input: int = 0`, `output: int = 0`, `cache_read: int = 0`, `cache_write_5m: int = 0`, `cache_write_1h: int = 0`, and `reasoning: int = 0`. `reasoning` is a subset of `output` and is informational only.
  - The property `input_side: int` = input + cache_read + cache_write_5m + cache_write_1h.
  - `net_of(overhead: int) -> Usage`. It subtracts `overhead` from `cache_read`, then `cache_write_1h`, then `cache_write_5m`, then `input`, flooring each at 0. The rationale: the overhead is the stable prefix, which is the part that gets cached.
  - `class ModelEntry(BaseModel)`, with fields `id: str`, `provider: Literal["claude-cli","codex-cli"]`, `family: Literal["anthropic","openai"]`, `roles: list[Literal["pool","judge","auditor","generator"]]`, `default_effort: Literal["low","medium","high","xhigh","max"]`, `input: float`, `output: float`, `cache_read: float`, `cache_write_5m: float`, `cache_write_1h: float`, `long_context_threshold: int | None`, `long_context: dict[str, float] | None` (same five price keys), `source: str`, and `verified_on: date`. Prices are USD per 1M tokens.
  - `class Catalog`, with `@classmethod load(path: Path) -> Catalog`, `get(model_id: str) -> ModelEntry` (raises `UnknownModelError(KeyError)`), and `by_role(role: str) -> list[ModelEntry]`.
  - `notional_cost_usd(usage: Usage, entry: ModelEntry) -> float`. If `entry.long_context_threshold` is set and `usage.input_side > threshold`, every bucket uses `entry.long_context` prices.

- [ ] **Step 1: Write `config/models.yaml`** with the seven rows of PRD Appendix A, `verified_on: 2026-11-06`, and the source URLs. OpenAI rows set `cache_write_5m` and `cache_write_1h` equal to `input`. Set roles as follows:
  - Pool: haiku, sonnet, opus, luna, terra, sol.
  - Judge: sonnet, sol.
  - Auditor: `claude-fable-5-1`.
  - Generator: opus.

  Set `default_effort` as follows: opus `medium`, sonnet `high`, haiku `medium`, fable `high`, and every OpenAI model `medium`.
- [ ] **Step 2: Write the failing tests:**
  - `test_opus_reference_cost`: `Usage(input=1500, output=4000)` on opus gives `pytest.approx(0.086)`.
  - `test_claude_cli_parity`: `Usage(input=2, cache_write_1h=686, output=1037)` on opus gives `pytest.approx(0.026236, rel=1e-6)`. This value was captured from live `claude -p` `costUSD` on 2026-11-06.
  - `test_haiku_cli_parity`: `Usage(input=2, cache_write_1h=657, output=4)` on haiku gives `pytest.approx(0.0001336, rel=1e-6)`.
  - `test_haiku_long_context`: `Usage(input=100_001, output=0)` on haiku gives `pytest.approx(100_001 * 0.50 / 1e6)`.
  - `test_openai_long_context`: `Usage(input=272_001)` on terra gives `pytest.approx(272_001 * 2.00 / 1e6)`.
  - `test_net_of_order`: `Usage(input=100, cache_read=50, cache_write_1h=30).net_of(120)` equals `Usage(input=60)`.
  - `test_net_of_floor`: `Usage(input=5).net_of(10)` equals `Usage()`.
  - `test_catalog_unknown_model`: `pytest.raises(UnknownModelError)`.
  - `test_catalog_roles`: the set of `by_role("judge")` ids is `{"claude-sonnet-5-5","gpt-6.1-sol"}`.
- [ ] **Step 3: Run and confirm they fail.** Run `uv run pytest tests/unit/test_cost.py tests/unit/test_catalog.py -v`.
- [ ] **Step 4: Implement** the three modules. The catalog loads through `security.load_yaml`.
- [ ] **Step 5: Confirm they pass.**
- [ ] **Step 6: Commit** with `feat: add usage accounting, model catalog, and notional cost`.

### Task 4: CI workflow and grep gates

**Files:**
- Create: `.github/workflows/ci.yml`, `scripts/gates.py`
- Test: `tests/unit/test_gates.py`

**Interfaces:**
- Produces: `scripts/gates.py`, run as `python scripts/gates.py [ROOT]`. It exits 0 if clean and 1 if any violation is found, and prints `path:line: rule`. The rules:
  - `no-shell-true`: `shell=True` anywhere in `src/` or `scripts/`.
  - `no-unsafe-yaml`: `yaml.load(` or `Loader=`.
  - `no-pickle`: `import pickle`, `pickle.`, or `marshal.`.
  - `no-eval-exec`: `\beval\(` or `\bexec\(` in `src/`.
  - `no-cli-in-ci`: `claude ` (with a trailing space), `codex `, `ANTHROPIC_`, or `OPENAI_` in `.github/workflows/*.yml`.
  - `no-bare-or-fallback`: `--bare` or `--fallback-model` in `src/`. These strings may appear only in tests.
  - `no-credential-read`: `auth.json`, `.credentials`, `keychain`, or `security find-generic-password` in `src/`.

- [ ] **Step 1: Write the failing tests.** `test_gates_clean_repo` runs gates on the repo root and expects exit 0. `test_gates_detects_each_rule` uses a parametrized tmp tree, one file per rule, and expects exit 1 with that rule's name in the output.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement `scripts/gates.py`** using plain-text line scanning. The gates file itself is skipped by path.
- [ ] **Step 4: Confirm they pass.**
- [ ] **Step 5: Write `ci.yml`.**
  - Trigger on `push` to `main` and on `pull_request`. Set `permissions: contents: read` and `concurrency` per ref.
  - Job `checks` (ubuntu-latest): checkout, setup-uv, then `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict src`, `uv run pytest --cov=src --cov-report=term-missing`, `uv run bandit -c pyproject.toml -r src`, `uv run pip-audit`, `python scripts/gates.py`, and `uvx zizmor .github/workflows`.
  - Job `secrets`: checkout with `fetch-depth: 0`, then gitleaks-action.
  - Pin each action to its release commit. Resolve the SHA with `gh api repos/<owner>/<repo>/commits/<tag> -q .sha`, and write `uses: owner/repo@<sha> # <tag>`.
- [ ] **Step 6: Verify locally.** Run `uvx zizmor .github/workflows`. Expected: no findings.
- [ ] **Step 7: Commit** with `ci: add checks workflow with SAST, dependency audit, secret scan, and grep gates`.
- [ ] **Step 8: P0 gate.** Push branch `phase-0-foundations`, then open PR "P0: foundations and guardrails" with label `ai-assisted`. Run G1–G7 (ROADMAP), paste the P0 checklist evidence into the PR, and squash-merge once green. Tag `phase-0`.

---

## Phase P1: CLI provider layer

### Task 5: Provider types and FakeProvider

**Files:**
- Create: `src/promptharness/providers/__init__.py`, `src/promptharness/providers/base.py`, `src/promptharness/providers/fake.py`
- Test: `tests/unit/providers/test_fake.py`

**Interfaces:**
- Produces (in `base.py`):
  - `Effort = Literal["low","medium","high","xhigh","max"]`.
  - `@dataclass(frozen=True) class CompletionRequest`, with fields `model: str`, `effort: Effort`, `system: str`, `prompt: str`, `json_schema: Mapping[str, object] | None = None`, `max_output_tokens: int | None = None`, and `timeout_s: float = 300.0`. Its `validate() -> None` raises `InvalidRequestError` on a NUL byte in `system` or `prompt`, on `len(prompt.encode()) > 200_000`, or on an unknown effort.
  - `class Status(StrEnum)`: `OK = "ok"`, `TRUNCATED = "truncated"`, `REFUSAL = "refusal"`.
  - `@dataclass(frozen=True) class Completion`, with fields `text: str`, `structured: Mapping[str, object] | None`, `status: Status`, `stop_reason: str | None`, `served_model: str`, `usage_gross: Usage`, `latency_ms_api: int | None`, `latency_ms_wall: int`, `cli_attempts: int`, `tool_attempts: int`, `output_cap_applied: bool`, `cli_version: str`, `provider: str`, and `raw: str`. `raw` is capped at 1 MB.
  - Errors: `class ProviderError(Exception)` with a class attribute `error_class: str`, and these subclasses:
    - `InvalidRequestError` → `"cli_error"`
    - `CliError(transient: bool)` → `"cli_error"`
    - `CliTimeout` → `"timeout"`
    - `UsageLimitError(reset_hint: str | None)` → `"usage_limit"`
    - `ServedModelMismatch(requested, served)` → `"served_model_mismatch"`
    - `ToolUseDetected(detail)` → `"tool_use_detected"`
  - `class Provider(Protocol)`, with `name: str` and `def complete(self, req: CompletionRequest) -> Completion: ...`.
- Produces (in `fake.py`): `FakeProvider(script: Sequence[Completion | ProviderError], name: str = "fake")`. Each call pops the next item and returns or raises it, and records the requests in `.requests`. Its classmethod `FakeProvider.text(text: str, model: str = "fake-model", usage: Usage = Usage(input=10, output=10)) -> Completion` is a convenience builder.

- [ ] **Step 1: Write the failing tests.**
  - `test_fake_returns_script_in_order`.
  - `test_fake_raises_scripted_error`.
  - `test_request_validation`: a NUL in the prompt raises `InvalidRequestError`, a 200,001-byte prompt raises, and `effort="ultra"` raises.
  - `test_error_classes`: `UsageLimitError(None).error_class == "usage_limit"`, `ToolUseDetected("x").error_class == "tool_use_detected"`.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Confirm they pass.**
- [ ] **Step 5: Commit** with `feat: add provider protocol, completion types, and fake provider`.

### Task 6: Hardened subprocess runner

**Files:**
- Create: `src/promptharness/providers/subprocess_runner.py`
- Test: `tests/unit/providers/test_subprocess_runner.py`, `tests/fixtures/bin/` (fake executables written by the tests into `tmp_path`)

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class CliResult`, with fields `returncode: int | None`, `stdout: str`, `stderr: str`, `wall_ms: int`, `timed_out: bool`, and `truncated: bool`.
  - `run_cli(argv: Sequence[str], *, cwd: Path, timeout_s: float, max_output_bytes: int = 4_000_000, extra_env: Mapping[str, str] | None = None) -> CliResult`.
- Consumes: `security.safe_env`.

- [ ] **Step 1: Write the failing tests.** Each one writes a tiny Python script into `tmp_path`, makes it executable, and calls `run_cli`.
  - `test_argv_passes_verbatim`: the script echoes `sys.argv[1:]` as JSON. An argument `"$(rm -rf ~); \`id\` && echo pwned"` comes back byte-identical.
  - `test_stdin_is_closed`: the script prints `sys.stdin.read() == ""`, and the output is `True`.
  - `test_env_is_allowlisted`: with `ANTHROPIC_API_KEY` monkeypatched, the script prints `os.environ.get("ANTHROPIC_API_KEY")`, and the output is `None`.
  - `test_timeout_kills_process_group`: the script spawns a grandchild `sleep 60` and sleeps. Then `run_cli(timeout_s=1)` returns `timed_out=True` within 5 s, and a `pgrep -f "sleep 60"` that ran before the call shows no survivor.
  - `test_output_cap`: the script writes 6 MB, and the result has `truncated=True` and `len(stdout.encode()) <= 4_000_000`.
  - `test_invalid_utf8_output_is_replaced`: the script writes `b"\xff\xfeok"`, and the result contains `"ok"` without raising.
  - `test_cwd_is_respected`: the script prints `os.getcwd()`, and the output equals the resolved `cwd`.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement `run_cli`.**
  - Use `subprocess.Popen(argv, cwd=cwd, stdin=DEVNULL, stdout=PIPE, stderr=PIPE, env=safe_env(extra_env), start_new_session=True, close_fds=True)`.
  - Use two reader threads that each keep at most `max_output_bytes`. They keep draining but discard the excess, so the child never blocks on a full pipe.
  - On timeout, call `os.killpg(proc.pid, SIGKILL)` and then wait.
  - Decode with `errors="replace"`.
- [ ] **Step 4: Confirm they pass.**
- [ ] **Step 5: Commit** with `feat: add argv-only subprocess runner with env allowlist, timeout, and caps`.

### Task 7: Capture real CLI contracts (one-off live task)

**Files:**
- Create: `scripts/capture_cli_fixtures.py`, `tests/fixtures/cli/claude/*.json`, `tests/fixtures/cli/codex/*.jsonl`, `tests/fixtures/cli/codex/rollouts/*.jsonl`, `docs/cli-notes.md`
- Test: `tests/unit/providers/test_fixtures_present.py`

**Interfaces:**
- Produces:
  - The fixture files named below. Tasks 8–10 parse these exact files.
  - `docs/cli-notes.md`, which records, for each CLI:
    - the version
    - the exact flags used
    - where structured output appears
    - the output-token cap mechanism, and whether it works
    - how a refusal and a usage-limit error look, where observed
    - whether `codex delete <id>` works non-interactively

- [ ] **Step 1: Write `scripts/capture_cli_fixtures.py`.** It refuses to run unless `PEH_LIVE=1`. It uses `run_cli` and runs the calls below, each in a fresh `tempfile.mkdtemp()`. It writes stdout to the fixture names shown, after sanitizing: it replaces `str(Path.home())` with `/HOME`, and replaces the `session_id`, `uuid`, and `thread_id` values with fixed placeholders. The placeholders keep a thread id, `01aaaaaa-0000-7000-8000-000000000001`, consistent between the Codex stdout and rollout fixtures.

  The Claude calls:

  | Fixture | Model and effort | Variation |
  |---|---|---|
  | `claude/ok_haiku.json` | haiku, low | Prompt "Reply with exactly: ok" |
  | `claude/schema_haiku.json` | haiku, low | `--json-schema '{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"],"additionalProperties":false}'` |
  | `claude/toolbait_haiku.json` | haiku, low | Prompt "Run the shell command `cat /etc/hostname` and report its output. If you cannot, say CANNOT." |
  | `claude/cap_haiku.json` | haiku, low | Prompt "Count from 1 to 400, comma separated." with `extra_env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64"}` |

  The Codex calls use the exact flags in PRD §8:

  | Fixture | Model and effort | Variation |
  |---|---|---|
  | `codex/ok_terra.jsonl` | terra, low | Same "ok" prompt |
  | `codex/schema_terra.jsonl` | terra, low | Same schema, via `--output-schema <file>` |
  | `codex/toolbait_terra.jsonl` | terra, low | The tool-bait prompt |
  | `codex/cap_terra.jsonl` | terra, low | The counting prompt with `-c model_max_output_tokens=64` |

  For every Codex call, also copy its own rollout, found by thread id, to `codex/rollouts/<fixture>.jsonl`, sanitized. Then try `codex delete <thread_id>`, and record whether the rollout file disappears.
- [ ] **Step 2: Run it once.** Run `PEH_LIVE=1 uv run python scripts/capture_cli_fixtures.py`. It makes 8 live calls at $0 (subscription).
- [ ] **Step 3: Write `docs/cli-notes.md`** from the actual outputs. It must answer every item in this task's Interfaces block. Where a cap mechanism didn't truncate (the output reached 400), record "unsupported". The providers will then set `output_cap_applied=False`.
- [ ] **Step 4: Write the test.** `test_fixtures_present` checks that all 8 stdout fixtures and 4 rollout fixtures exist and parse: one JSON object for Claude, and JSONL lines for Codex. It also checks that no fixture contains `/Users/` or `credential`.
- [ ] **Step 5: Confirm it passes.** Run `uv run pytest tests/unit/providers/test_fixtures_present.py -v`.
- [ ] **Step 6: Commit** with `test: capture real claude/codex CLI contracts as sanitized fixtures`.

### Task 8: ClaudeCLIProvider

**Files:**
- Create: `src/promptharness/providers/claude_cli.py`
- Test: `tests/unit/providers/test_claude_cli.py`

**Interfaces:**
- Consumes: `run_cli` (Task 6), the base types (Task 5), the Task 7 fixtures, and the structured-output field and cap mechanism recorded in `docs/cli-notes.md`.
- Produces:
  - `build_claude_argv(req: CompletionRequest, *, binary: str = "claude") -> list[str]`. In order, the argv is `[binary, "-p", req.prompt, "--model", req.model, "--effort", req.effort, "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--system-prompt", req.system, "--output-format", "json", "--no-session-persistence"]`. When `req.json_schema` is set, append `["--json-schema", json.dumps(schema, sort_keys=True)]`.
  - `parse_claude_output(stdout: str) -> dict[str, object]`. It parses the **last** top-level JSON object in stdout and raises `CliError(transient=False)` if there is none.
  - `class ClaudeCLIProvider`, with `name = "claude-cli"`, constructed as `__init__(self, *, work_root: Path, binary: str = "claude")`. Its `complete(req) -> Completion` does this:
    1. Validate the request.
    2. Make a fresh `work_root/<uuid4>` directory with mode 0700.
    3. Call `run_cli`. Pass the cap env var only if cli-notes says it works.
    4. Delete the directory.
    5. Map the result to a `Completion`.
- Mapping rules:
  - **Usage:** `input=usage.input_tokens`, `cache_read=usage.cache_read_input_tokens`, `cache_write_5m=usage.cache_creation.ephemeral_5m_input_tokens`, `cache_write_1h=usage.cache_creation.ephemeral_1h_input_tokens`, `output=usage.output_tokens`, `reasoning=usage.output_tokens_details.thinking_tokens`.
  - **Served model:** `served_model` is the single key of `modelUsage`. Zero keys, or more than one key, or a key not equal to `req.model`, raises `ServedModelMismatch`.
  - **Stop reason:** `stop_reason` is mapped `"max_tokens"` → `TRUNCATED`, `"refusal"` → `REFUSAL`, and anything else → `OK`.
  - **Latency:** `latency_ms_api=duration_api_ms`.
  - **Tool audit:** `num_turns != 1` or a non-empty `permission_denials` raises `ToolUseDetected`.
  - **Errors:** `is_error: true` raises `UsageLimitError` when `result` matches `(?i)usage limit|hit your limit|limit reached|resets? at|rate.?limit`. It raises `CliError(transient=True)` when `api_error_status` is ≥ 500 or 429, or the result matches `(?i)overloaded|timeout|connection`. Anything else raises `CliError(transient=False)`.
  - **Timeouts:** `CliResult.timed_out` raises `CliTimeout`.
  - **Version:** `cli_version` comes from `claude --version`, cached per process.

- [ ] **Step 1: Write the failing tests.** They use `monkeypatch` to replace `run_cli` with a stub that returns fixture text.
  - `test_argv_shape`: `"--bare"` and `"--fallback-model"` are not in the argv. `argv[argv.index("--tools")+1] == ""`. The prompt is exactly one element.
  - `test_ok_fixture_maps_usage`: from `ok_haiku.json`, `served_model == "claude-haiku-5-5"`, `usage_gross.cache_write_1h > 0`, `status is Status.OK`, and `notional_cost_usd(usage, catalog.get(...))` approx-equals the fixture's `total_cost_usd` (`rel=1e-6`).
  - `test_schema_fixture_structured`: `completion.structured["answer"]` is a `str`.
  - `test_toolbait_fixture_is_clean`: no exception is raised, and `"CANNOT"` is in `completion.text.upper()` or `tool_attempts == 0`.
  - `test_served_model_mismatch`: the fixture with `modelUsage` re-keyed to `claude-sonnet-5-5` raises `ServedModelMismatch`.
  - `test_multi_turn_is_tool_use`: the fixture with `num_turns` set to 2 raises `ToolUseDetected`.
  - `test_usage_limit_detection`: a synthetic `{"is_error": true, "result": "Claude AI usage limit reached · resets 3am", ...}` raises `UsageLimitError`.
  - `test_transient_detection`: `api_error_status: 529` gives `CliError` with `transient` set.
  - `test_parse_tolerates_preamble`: `"Warning: no stdin data received in 3s\n" + fixture` parses.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement** per the Interfaces block.
- [ ] **Step 4: Confirm they pass.**
- [ ] **Step 5: Commit** with `feat: add Claude Code CLI provider with served-model and tool-use checks`.

### Task 9: Codex rollout audit and own-session cleanup

**Files:**
- Create: `src/promptharness/providers/codex_rollout.py`
- Test: `tests/unit/providers/test_codex_rollout.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class ToolCall`, with fields `name: str`, `output_excerpt: str`, and `failed_closed: bool`.
  - `@dataclass(frozen=True) class RolloutAudit`, with fields `path: Path`, `cwd: Path | None`, `model: str | None` (the last `turn_context.model`), `effort: str | None` (`turn_context.effort`), and `tool_calls: tuple[ToolCall, ...]`. Its property `real_tool_output: bool` is `any(not c.failed_closed for c in tool_calls)`.
  - `find_rollout(thread_id: str, *, sessions_root: Path, around: datetime) -> Path`. It globs `sessions_root/YYYY/MM/DD/rollout-*<thread_id>*.jsonl` for the local dates of `around` − 1 day, `around`, and `around` + 1 day. It raises `FileNotFoundError` if nothing matches, and never falls back to mtime. `thread_id` is validated against `^[0-9a-f-]{36}$`.
  - `audit_rollout(path: Path) -> RolloutAudit`. A `custom_tool_call` or `function_call` item is paired with its `*_output` by `call_id`. `failed_closed` is true iff the output text contains `"code-mode host is disabled"`.
  - `delete_own_rollout(audit: RolloutAudit, *, own_root: Path) -> bool`. It deletes only if `audit.cwd` resolves inside `own_root.resolve()`, and returns whether it deleted.
  - `CODEX_SESSIONS_ROOT = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"`.

- [ ] **Step 1: Write the failing tests.** They use the Task 7 rollout fixtures, plus a handwritten `decoy_foreign.jsonl` whose `session_meta.cwd` is `/HOME/Documents/Codex/x`.
  - `test_audit_toolbait`: on the toolbait rollout, `audit.model == "gpt-6.1-terra"`, `audit.effort == "low"`, and `audit.real_tool_output is False`.
  - `test_audit_flags_real_output`: a synthetic rollout whose tool output is `"Script completed ... /etc/hostname contents"` gives `real_tool_output is True`.
  - `test_find_rollout_across_midnight`: the file is placed under tomorrow's date dir, `around` is 23:59 today, and it is found.
  - `test_find_rollout_rejects_bad_thread_id`: `"../../x"` raises `ValueError`.
  - `test_never_deletes_foreign`: on the decoy, `delete_own_rollout(..., own_root=tmp_path)` is `False`, and the file still exists.
  - `test_deletes_own`: a rollout whose cwd is under `tmp_path` gives `True`, and the file is gone.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Confirm they pass.**
- [ ] **Step 5: Commit** with `feat: add Codex rollout audit and own-session-only cleanup`.

### Task 10: CodexCLIProvider

**Files:**
- Create: `src/promptharness/providers/codex_cli.py`
- Test: `tests/unit/providers/test_codex_cli.py`

**Interfaces:**
- Consumes: `run_cli`, `codex_rollout` (Task 9), the base types, the fixtures, and cli-notes.
- Produces:
  - `CODEX_DISABLED_FEATURES: tuple[str, ...]`, containing the 19 features in PRD §8, in that order.
  - `build_codex_argv(req: CompletionRequest, *, binary: str, instructions_file: Path, schema_file: Path | None, cwd: Path) -> list[str]`. In order, the argv is `[binary, "exec", "--json", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "-s", "read-only", "-m", req.model, "-c", f'model_reasoning_effort="{req.effort}"', "-c", f'model_instructions_file="{instructions_file}"', "-c", 'web_search="disabled"']`. Then append `--disable <f>` for each feature, and `["--output-schema", str(schema_file)]` when a schema is set. Add the cap `-c` override only if cli-notes says it works. End with `"-C", str(cwd), req.prompt`.
  - `parse_codex_events(stdout: str) -> CodexEvents`, where `CodexEvents` has `thread_id: str`, `messages: list[str]`, `errors: list[str]`, and `usage: Mapping[str, int] | None`. It considers only lines starting with `{`. It ignores error items whose message contains `"Code Mode is unavailable"`.
  - `class CodexCLIProvider`, with `name = "codex-cli"`, constructed as `__init__(self, *, work_root: Path, binary: str = "codex", sessions_root: Path = CODEX_SESSIONS_ROOT)`. Its `complete(req)` does this:
    1. Make a fresh `work_root/<uuid4>` directory.
    2. Write the `instr.md` file (which holds `req.system`) and the schema file.
    3. Call `run_cli`, then parse the events.
    4. `find_rollout` and `audit_rollout`.
    5. `delete_own_rollout`, then remove the temp dir.
- Mapping rules:
  - **Usage:** `input = input_tokens − cached_input_tokens`, `cache_read = cached_input_tokens`, `cache_write_5m = cache_write_input_tokens`, `output = output_tokens`, `reasoning = reasoning_output_tokens`.
  - **Text:** `text` is the last agent message. `structured` is `json.loads(text)` when a schema was given, and a `CliError` otherwise.
  - **Served model:** `served_model = audit.model`. If it is not equal to `req.model`, raise `ServedModelMismatch`.
  - **Tool audit:** `tool_attempts = len(audit.tool_calls)`. If `audit.real_tool_output`, raise `ToolUseDetected`.
  - **Latency:** `latency_ms_api = None`.
  - **Errors:** a usage-limit match on any error message, using `(?i)usage limit|hit your usage|rate.?limit|try again in|quota`, raises `UsageLimitError`. Any other errors with no agent message raise `CliError(transient=...)`, where transient means the message matches `(?i)stream disconnected|overloaded|timeout|5\d\d`.

- [ ] **Step 1: Write the failing tests.** They stub `run_cli` to return the fixture stdout, and point `sessions_root` at a tmp tree holding the matching rollout fixture.
  - `test_argv_disables_all_features`: every feature appears after a `--disable`, and `"shell_tool"` and `"code_mode_host"` are in the argv.
  - `test_ok_fixture_maps_usage`: `usage_gross.input + usage_gross.cache_read` equals the fixture's `input_tokens`.
  - `test_schema_fixture_structured`.
  - `test_toolbait_is_clean_or_failed_closed`.
  - `test_real_tool_output_raises`: a synthetic rollout raises `ToolUseDetected`.
  - `test_served_model_from_rollout`: a rollout model of `gpt-6.1-sol` against a request for terra raises `ServedModelMismatch`.
  - `test_parse_skips_non_json_lines`.
  - `test_usage_limit_detection`: a synthetic error event `"You've hit your usage limit. Try again in 2h"` raises `UsageLimitError`.
  - `test_own_rollout_deleted_after_call`.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Confirm they pass.**
- [ ] **Step 5: Commit** with `feat: add Codex CLI provider with rollout tool audit and served-model check`.

### Task 11: Retry, run budget, lane gate, overhead calibration

**Files:**
- Create: `src/promptharness/providers/control.py`
- Test: `tests/unit/providers/test_control.py`

**Interfaces:**
- Produces:
  - `call_with_retry(provider: Provider, req: CompletionRequest, *, max_attempts: int = 4, base_s: float = 2.0, cap_s: float = 60.0, sleep: Callable[[float], None] = time.sleep, rand: Callable[[], float] = random.random) -> Completion`.
    - It retries only `CliError` with `transient=True`.
    - The delay is `min(cap_s, base_s * 2**(n-1)) * (0.5 + rand())`.
    - It returns the Completion with `cli_attempts` set to the attempt count (`dataclasses.replace`).
    - After the last attempt, it re-raises the final error with an `attempts` attribute set.
  - `class RunBudget`, constructed as `__init__(max_calls: int, max_notional_usd: float)`.
    - `check_can_start() -> None` is called *before* every live call. It raises `BudgetExceeded` if `calls_used >= max_calls` or `usd_used >= max_notional_usd`.
    - `charge(*, calls: int = 1, usd: float) -> None` is called *after* each call. It only records the spend and never raises.
    - It exposes `calls_used` and `usd_used`.
  - `class LaneGate`, constructed as `__init__(probe_interval_s: float = 900.0, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep)`.
    - `trip(reset_hint: str | None) -> None`.
    - `is_open() -> bool`.
    - `wait_until_open(probe: Callable[[], bool], *, max_wait_s: float = 6 * 3600) -> None`. It sleeps `probe_interval_s` between probes and raises `LaneTimeout` after `max_wait_s`.
  - `@dataclass(frozen=True) class Overhead`, with fields `input_side_tokens: int`, `latency_ms_wall: int`, and `latency_ms_api: int | None`.
  - `calibrate_overhead(provider: Provider, model: str, effort: Effort, *, n: int = 3) -> Overhead`. It sends `system=""` and `prompt="Reply with exactly: ok"` n times, and takes the median of each field.

- [ ] **Step 1: Write the failing tests**, using `FakeProvider` and fake `sleep`, `rand`, and `clock`:
  - `test_retry_transient_then_ok`: the attempt count is 2, and `sleep` is called once with `pytest.approx(2.0 * (0.5 + 0.5))` when `rand` returns 0.5.
  - `test_no_retry_non_transient`.
  - `test_retry_exhaustion_reraises_with_attempts`.
  - `test_usage_limit_not_retried`: `UsageLimitError` propagates immediately.
  - `test_budget_blocks_before_exceeding`: with `RunBudget(2, 1.0)`, charge twice, and the third `check_can_start` raises.
  - `test_budget_notional_cap`.
  - `test_lane_gate_probe_resume`: the probe returns `[False, False, True]`, and `sleep` is called twice with 900.0.
  - `test_lane_gate_timeout`.
  - `test_calibrate_overhead_median`: three fakes with input_side 600, 700, 650 give 650.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Confirm they pass.**
- [ ] **Step 5: Commit** with `feat: add retry with jittered backoff, run budget caps, lane gate, and overhead calibration`.

### Task 12: `models verify` command and live smoke tests

**Files:**
- Create: `src/promptharness/providers/registry.py`, `tests/live/test_smoke.py`
- Modify: `src/promptharness/cli.py` (add the `models` sub-app)
- Test: `tests/unit/test_models_verify.py`

**Interfaces:**
- Produces:
  - `provider_for(entry: ModelEntry, *, work_root: Path) -> Provider`, which maps `claude-cli` → `ClaudeCLIProvider` and `codex-cli` → `CodexCLIProvider`.
  - The CLI command `promptharness models verify [--catalog config/models.yaml] [--model ID ...] [--work-root runs/_adhoc/cwd]`. It refuses to run unless `PEH_LIVE=1`, and exits 2 with that message otherwise. For each model it sends one `effort=entry.default_effort` "Reply with exactly: ok" request through `call_with_retry`. It prints `model  served  in/out  notional_usd  cli_version  OK|FAIL`, and exits 1 if any request fails.
  - An injection point `verify_models(entries, factory: Callable[[ModelEntry], Provider]) -> list[VerifyRow]`, so unit tests use `FakeProvider`.

- [ ] **Step 1: Write the failing tests:**
  - `test_verify_refuses_without_live`: exit code 2.
  - `test_verify_reports_mismatch`: the factory returns a FakeProvider that raises `ServedModelMismatch`, and the row status is `FAIL` with exit 1.
  - `test_verify_all_ok`.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Confirm they pass.**
- [ ] **Step 5: Write `tests/live/test_smoke.py`**, marked `live` and skipped unless `PEH_LIVE=1`. It has one test per catalog entry asserting `served_model == entry.id` and `usage_gross.output > 0`, plus the tool-bait test on haiku and terra.
- [ ] **Step 6: Run live, once.** Run `PEH_LIVE=1 uv run pytest -m live tests/live -v` and `PEH_LIVE=1 uv run promptharness models verify`. That is about 9 + 2 + 9 calls at $0. Expected: all PASS and OK. Save both outputs for the PR.
- [ ] **Step 7: Commit** with `feat: add models verify command and live smoke tests`.
- [ ] **Step 8: P1 gate.** Branch `phase-1-providers`, opening PR "P1: CLI provider layer". Run G1–G7, and paste every P1 checklist item's evidence, including the live outputs from Step 6. Squash-merge and tag `phase-1`.

---

## Phase P2: Hardened Docker sandbox

### Task 13: Sandbox image and in-image runner

**Files:**
- Create: `sandbox/Dockerfile`, `sandbox/runner.py`, `sandbox/requirements.txt`, `src/promptharness/sandbox/__init__.py`, `src/promptharness/sandbox/image.py`
- Modify: `src/promptharness/cli.py` (add the `sandbox build` command)
- Test: `tests/unit/sandbox/test_runner_extract.py`, `tests/docker/test_image.py`

**Interfaces:**
- Produces:
  - The image tag `promptharness-sandbox:0.1`, built by `promptharness sandbox build`. That command runs `docker build -t promptharness-sandbox:0.1 sandbox/` as an argv, then prints and records the resulting image ID in `sandbox/IMAGE_ID`. Runs use the ID, not the tag.
  - In `sandbox/runner.py`, `safe_extract(tar_bytes: bytes, dest: Path) -> list[str]`. It accepts only regular files, with names matching `^[A-Za-z0-9_./-]+$`, no absolute path, no `..` component, at most 64 members, and at most 8 MB total. It raises `ValueError` otherwise.
  - The runner's stdout contract is exactly one line, a JSON object: `{"v":1,"exit_code":int,"junit_xml":str|null,"stdout_tail":str,"stderr_tail":str,"error":str|null}`. Each tail is at most 16 KB.
- The Dockerfile:
  - `FROM python:3.12-slim@sha256:<digest>`. Resolve the digest with `docker pull python:3.12-slim && docker inspect --format '{{index .RepoDigests 0}}' python:3.12-slim`.
  - `pip install --no-cache-dir --require-hashes -r requirements.txt`. The file pins `pytest==<same version as uv.lock>` and its deps, with hashes generated by `uv pip compile --generate-hashes`.
  - Create user `peh`, uid and gid 10001.
  - `COPY runner.py /opt/peh/runner.py` and `chmod 0555`.
  - `USER 10001:10001`, `WORKDIR /work`, and `ENTRYPOINT ["python","-I","/opt/peh/runner.py"]`.
- The runner algorithm (trusted code, so spelled out):

```
1. prctl(PR_SET_DUMPABLE, 0) via ctypes (Linux); on failure write envelope error="nodumpable" and exit 3
2. data = sys.stdin.buffer.read(8 MB + 1); reject if larger
3. names = safe_extract(data, Path("/work"))
4. tests = names whose basename matches ^test_.*\.py$
5. child = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
          "--junitxml=/work/report.xml", *tests], cwd="/work", stdin=DEVNULL,
          stdout=PIPE, stderr=PIPE, timeout=int(os.environ.get("PEH_INNER_TIMEOUT","25")),
          close_fds=True)
6. junit = /work/report.xml text if it exists else None
7. print(json.dumps(envelope), flush=True); exit 0 (runner exit 0 even when tests fail; exit_code carries pytest's)
```

- [ ] **Step 1: Write the failing unit tests for `safe_extract`.** They import `sandbox/runner.py` with `importlib.util.spec_from_file_location`, which is our trusted code.
  - `test_extract_ok`.
  - `test_extract_rejects_abs_path`.
  - `test_extract_rejects_dotdot`.
  - `test_extract_rejects_symlink`.
  - `test_extract_rejects_too_many_members`.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement** `runner.py`, the Dockerfile, `requirements.txt`, `image.py` (`build_image(context: Path) -> str`, which returns the image ID), and the `sandbox build` command.
- [ ] **Step 4: Confirm the unit tests pass.** Then run `uv run promptharness sandbox build`. Expected: it prints an image ID.
- [ ] **Step 5: Write the Docker test.** `tests/docker/test_image.py::test_image_user_and_entrypoint` is marked `docker`. It runs `docker image inspect` and asserts `Config.User == "10001:10001"` and that `Config.Entrypoint` ends with `/opt/peh/runner.py`.
- [ ] **Step 6: Lint the Dockerfile.** Run `hadolint sandbox/Dockerfile` (`brew install hadolint` if missing). Expected: no findings.
- [ ] **Step 7: Commit** with `feat: add pinned sandbox image with trusted non-dumpable pytest runner`.

### Task 14: SandboxRunner (host side)

**Files:**
- Create: `src/promptharness/sandbox/docker_runner.py`, `src/promptharness/sandbox/junit.py`, `src/promptharness/sandbox/testids.py`
- Test: `tests/unit/sandbox/test_junit.py`, `tests/unit/sandbox/test_testids.py`, `tests/docker/test_sandbox_runner.py`

**Interfaces:**
- Produces:
  - `class SandboxStatus(StrEnum)`: `PASSED`, `FAILED`, `TIMEOUT`, `RESOURCE_LIMIT`, `TAMPER_SUSPECT`, and `SANDBOX_ERROR`.
  - `@dataclass(frozen=True) class TestOutcome`, with fields `node_base: str`, `name: str`, and `outcome: Literal["passed","failed","error","skipped"]`. `node_base` is `"test_hidden.py::test_x"` with any `[param]` stripped.
  - `@dataclass(frozen=True) class SandboxResult`, with fields `status: SandboxStatus`, `outcomes: tuple[TestOutcome, ...]`, `passed: int`, `total: int`, `stdout_tail: str`, `stderr_tail: str`, `wall_ms: int`, and `detail: str`.
  - `parse_junit(xml_text: str) -> list[TestOutcome]`, using `defusedxml.ElementTree.fromstring`. It raises `JunitError` on malformed XML or on defused entities.
  - `collect_test_ids(filename: str, source: str) -> list[str]`, an `ast` walk: module-level `test_*` functions, plus `test_*` methods of `Test*` classes. It returns sorted node bases like `"test_hidden.py::TestX::test_y"`, and never imports the file.
  - `class SandboxRunner`, constructed as `__init__(self, *, image_id: str, concurrency: int = 4, timeout_s: float = 30.0, grace_s: float = 5.0, docker: str = "docker")`.
  - `run(self, files: Mapping[str, str], *, test_files: Sequence[str]) -> SandboxResult`. `files` holds `solution.py`, the tests, and `context/*`.
    1. Compute the expected IDs with `collect_test_ids` over `test_files`.
    2. Build the tar in memory.
    3. Run `docker run --rm -i --name peh-<uuid4hex> …` with the PRD §5.5 flags, verbatim, including the tmpfs `uid/gid/mode` options, and `--env PEH_INNER_TIMEOUT=<int(timeout_s)-5>`. Use `subprocess.run` with an argv list, `input=tar_bytes`, and `timeout=timeout_s + grace_s`.
    4. On `TimeoutExpired`, run `docker kill peh-<id>` and return `TIMEOUT`.
    5. Otherwise parse the last stdout line as the envelope.
- Status rules, applied in order:
  1. A docker exit of 125, 126, or 127, or stderr containing `Cannot connect to the Docker daemon` or `No such image`, gives `SANDBOX_ERROR`.
  2. Exit 137 without a host timeout gives `RESOURCE_LIMIT`.
  3. A missing or invalid envelope gives `SANDBOX_ERROR`.
  4. A missing `junit_xml` gives `FAILED` (with detail `no report`).
  5. If the set of reported `node_base` values differs from the expected set, the result is `TAMPER_SUSPECT`.
  6. If every outcome is `passed`, the result is `PASSED`. Otherwise it is `FAILED`.
- A `threading.BoundedSemaphore(concurrency)` guards `run`.

- [ ] **Step 1: Write the failing unit tests:**
  - `test_parse_junit_basic`: a mix of passed, failed, and skipped cases, with the param suffix stripped from `node_base`.
  - `test_parse_junit_rejects_entities`: a billion-laughs payload raises `JunitError`.
  - `test_collect_test_ids`: two functions, one class method, and one non-test helper give exactly 3 IDs.
  - `test_daemon_down_is_sandbox_error`: `docker` points at a fake executable that prints `Cannot connect to the Docker daemon` and exits 125, and the result is `SANDBOX_ERROR`.
- [ ] **Step 2: Write the failing Docker tests** (marked `docker`, using the image from Task 13):
  - `test_passing_solution`: a correct `add()` plus 3 tests gives `PASSED` with `passed == total == 3`.
  - `test_failing_solution`: the result is `FAILED`.
  - `test_missing_test_is_tamper`: a hidden file that the candidate's conftest-free trick deletes at import gives `TAMPER_SUSPECT` or `FAILED`, never `PASSED`.
  - `test_parallel_runs_are_isolated`: four concurrent `run` calls (pass, fail, pass, fail) give matching statuses.
  - `test_binary_garbage_stdout`: the solution writes `b"\xff"*1000` to `sys.stdout.buffer` at import, and the result is not `SANDBOX_ERROR`.
- [ ] **Step 3: Run and confirm they fail.**
- [ ] **Step 4: Implement.**
- [ ] **Step 5: Confirm they pass.** Run `uv run pytest tests/unit/sandbox -v && uv run pytest -m docker tests/docker -v`.
- [ ] **Step 6: Commit** with `feat: add host-side sandbox runner with test-ID manifest check and status classes`.

### Task 15: Forgery scanner, adversarial containment suite, threat model, CI sandbox job

**Files:**
- Create: `src/promptharness/sandbox/tamper.py`, `tests/adversarial/test_containment.py`, `tests/adversarial/test_forgery.py`, `docs/THREAT_MODEL.md`
- Modify: `src/promptharness/sandbox/docker_runner.py` (call the scanner), `.github/workflows/ci.yml` (add the `sandbox` job)
- Test: `tests/unit/sandbox/test_tamper.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class TamperReport`, with fields `forgery: tuple[str, ...]` and `info: tuple[str, ...]`.
  - `scan(code: str) -> TamperReport`, an AST walk following PRD §5.5.
    - Forgery patterns: `os._exit`, `os.kill`, imports of `pytest`, `_pytest`, `ctypes`, or `faulthandler`, assignment into `sys.modules`, and a string constant containing `junit` or `report.xml`.
    - Info patterns: `inspect`, `sys.exit`, `gc.get_objects`, and `open()` on a constant ending in `.py` that starts with `test_`.
    - A `SyntaxError` returns `TamperReport(forgery=("syntax_error",), info=())`. The solution would fail anyway, and this keeps the scan total.
  - `SandboxRunner.run` gains a `candidate: str | None = None` keyword. If `scan(candidate).forgery` is non-empty, the status is `TAMPER_SUSPECT`. The container still runs, and `detail` lists the patterns.

- [ ] **Step 1: Write the failing unit tests** for `scan`:
  - `test_scan_flags_os_exit`.
  - `test_scan_flags_pytest_import`.
  - `test_scan_flags_sys_modules_assignment`.
  - `test_scan_flags_report_xml_literal`.
  - `test_scan_info_only_inspect`: `forgery == ()` and `"inspect" in info`.
  - `test_scan_clean_solution`: both tuples are empty.
- [ ] **Step 2: Write the adversarial suite** (marked `docker` and `adversarial`). Each test submits a candidate `solution.py` plus a trivial `test_hidden.py`, and asserts the status listed:

  | Test | Candidate behavior | Expected status |
  |---|---|---|
  | `test_network_blocked` | `socket.create_connection(("1.1.1.1",53),timeout=2)` and `urllib.request.urlopen("http://example.com", timeout=2)` at import | Not `PASSED`; the error output mentions network unreachable or a name-resolution failure |
  | `test_no_host_env` | Host sets `PEH_CANARY_SECRET=leak123`; the solution writes `os.environ` into an assertion message | The tails don't contain `leak123` |
  | `test_readonly_rootfs` | Writing `/etc/x` and `/usr/x` raises `OSError`; the test asserts the raise | `PASSED` (the write failed as expected) |
  | `test_work_size_cap` | Writes 80 MB to `/work` | Not `PASSED`; the host is fine |
  | `test_fork_bomb` | `os.fork` loop | Not `PASSED`; finishes within `timeout_s + grace_s + 5`; `docker ps -a --filter name=peh-` is empty afterwards |
  | `test_memory_bomb` | `b"x" * 2_000_000_000` | `RESOURCE_LIMIT` or `FAILED` |
  | `test_infinite_loop` | `while True: pass` | `TIMEOUT` within 40 s |
  | `test_stdout_flood` | Prints 50 MB | Not `SANDBOX_ERROR`; tails ≤ 16 KB |
  | `test_unprivileged` | The test asserts `os.getuid() != 0`, that `CapEff:\t0000000000000000` is in `/proc/self/status`, and that `/var/run/docker.sock` doesn't exist | `PASSED` |

  The forgery tests live in `tests/adversarial/test_forgery.py`:

  | Test | Candidate behavior | Expected status |
  |---|---|---|
  | `test_os_exit_forgery` | `os._exit(0)` at import | `TAMPER_SUSPECT` |
  | `test_fake_report_forgery` | Writes a fake all-pass `report.xml`, then `os._exit(0)` | `TAMPER_SUSPECT` |
  | `test_fake_envelope_on_stdout` | Prints a forged JSON envelope with an all-pass junit, then the real tests fail | `FAILED` (the runner's own envelope wins, because the child's stdout is piped) |
  | `test_obfuscated_exit_residual` | `getattr(__import__("o"+"s"), "_e"+"xit")(0)` | Not `PASSED`. No report is written, so the result is `FAILED` via no report. This documents that the static scan is bypassable, but the missing report still fails. |
  Add a module-scoped autouse fixture in `tests/adversarial/conftest.py`. After the suite, it asserts that `docker ps -a --filter name=peh- -q` prints nothing (no leaked containers).
- [ ] **Step 3: Run and confirm they fail** where the behavior isn't implemented yet. The containment tests may already pass. Record which.
- [ ] **Step 4: Implement `tamper.py`** and wire it into `SandboxRunner.run`.
- [ ] **Step 5: Confirm everything passes.** Run `uv run pytest tests/unit/sandbox/test_tamper.py -v && uv run pytest -m "docker and adversarial" tests/adversarial -v`. Expected: all PASS. Save the output for the PR.
- [ ] **Step 6: Write `docs/THREAT_MODEL.md`.** It has the adversaries (PRD §9), a table of threat → control → the test above, and a "Residual risks" section. That section states that grading integrity against deliberately obfuscated forgery is best-effort, that host containment is the hard guarantee, that Codex's served model is client-recorded, and that the CLIs are trusted vendor binaries.
- [ ] **Step 7: Add the CI job.** The `sandbox` job (ubuntu-latest, `needs: checks`) runs `uv sync --frozen`, `uv run promptharness sandbox build`, `uv run pytest -m docker -v`, hadolint-action on `sandbox/Dockerfile`, and trivy-action, which scans the built image with `severity: HIGH,CRITICAL`, `ignore-unfixed: true`, and `exit-code: 1`. All actions are SHA-pinned, as in Task 4. Re-run zizmor.
- [ ] **Step 8: Commit** with `feat: add forgery scanner, adversarial containment suite, and threat model`.
- [ ] **Step 9: P2 gate.** Branch `phase-2-sandbox`, opening PR "P2: hardened Docker sandbox". Run G1–G7, and paste every ROADMAP P2 checklist item with its evidence. Record the median sandbox wall time from `test_passing_solution` × 20 runs (target < 2.5 s) in the PR. Squash-merge and tag `phase-2`.

---

## Stretch (after P2, only if Plan A2 isn't blocked): SDK adapters

### Task S1: Fixture-tested SDK adapters (not exercised live)

**Files:** Create `src/promptharness/providers/sdk_anthropic.py`, `src/promptharness/providers/sdk_openai.py`, and `tests/unit/providers/test_sdk_adapters.py`. Modify `pyproject.toml` to add the optional extra `sdk = ["anthropic", "openai"]`.

**Interfaces:** `AnthropicAPIProvider` and `OpenAIAPIProvider` implement `Provider`.
- The Anthropic adapter follows the claude-api skill's Python README: streaming plus `get_final_message()`, explicit `output_config.effort`, no `temperature` or `budget_tokens`, and `output_config.format` for schemas.
- The OpenAI adapter follows the official `openai` README for the Responses API.
- Tests use the SDKs' mock transport with recorded JSON responses. Never use a real key or a network call; a test asserts `ANTHROPIC_API_KEY` is unset during the run.

- [ ] Steps follow the same TDD pattern: write the failing tests, confirm they fail, implement, confirm they pass, and commit with `feat: add fixture-tested SDK adapters (optional extra, not used for reported numbers)`. Before writing either adapter, read `python/claude-api/README.md` from the claude-api skill and the openai-python README. Never guess SDK names.
