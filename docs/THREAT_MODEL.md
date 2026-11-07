# Threat model

Scope: the hardened Docker sandbox that runs LLM-written `solution.py` against hidden pytest suites (PRD 5.5 and 9). Host-side CLI, CI and dashboard controls are listed in PRD 9 and are only summarised here.

## Adversaries

| # | Adversary | Capability | Goal |
|---|---|---|---|
| A | LLM-generated code and tests | Arbitrary Python executed inside the container as uid 10001 | Escape or damage the host, exfiltrate data, exhaust resources, fake a passing grade |
| B | LLM output fed to judges | Free text that reaches a judge prompt | Flip a verdict by injection |
| C | Untrusted external data (PR titles, case metadata) | Strings that reach the dashboard or CLI arguments | XSS, command injection, path traversal |
| D | Supply chain | Malicious dependency, image layer or Action | Run attacker code in CI or on the owner's machine |

This document goes deep on adversary A. The others are covered by the PRD 9 controls (nonce-fenced judge input, `yaml.safe_load` and `defusedxml`, argv-only subprocesses, SHA-pinned Actions, `pip-audit`, Trivy and hadolint on the image).

## Sandbox threats, controls and tests

All tests are in `tests/adversarial/` and are marked `docker` and `adversarial`.

| Threat | Control | Test |
|---|---|---|
| Network exfiltration or attack | `--network none` | `test_containment.py::test_network_blocked` |
| Reading host secrets from the environment | No env passthrough; `safe_env`; the container receives only fixed variables | `test_containment.py::test_no_host_env` |
| Writing outside the work dir | `--read-only` rootfs, tmpfs `/work` and `/tmp` | `test_containment.py::test_readonly_rootfs` |
| Filling the host disk | tmpfs size cap (`/work` 64 MB), `--ulimit fsize` | `test_containment.py::test_work_size_cap` |
| Fork bomb | `--pids-limit 128`, inner and host wall-clock kill, `--rm` plus `docker kill` | `test_containment.py::test_fork_bomb` |
| Memory exhaustion | `--memory 512m --memory-swap 512m` (OOM kill) | `test_containment.py::test_memory_bomb` |
| CPU spin | `--cpus 1`, inner timeout, host timeout and `docker kill` | `test_containment.py::test_infinite_loop` |
| Output flood | Runner keeps only a 16 KB tail per stream and drains pipes | `test_containment.py::test_stdout_flood` |
| Privilege escalation | uid 10001, `--cap-drop ALL`, `no-new-privileges`, default seccomp, no Docker socket | `test_containment.py::test_unprivileged` |
| Leaked containers after any of the above | Unique `--name peh-<uuid>`, `--rm`, kill on host timeout | module autouse finalizer in `tests/adversarial/conftest.py` (`docker ps -a --filter name=peh-` must be empty) |
| Grader forgery by exiting early | Static scan rejects `_exit` on any base, `os.kill`/`os.killpg`, and process replacement (`execv`, `execve`, `execl`, `execle`, `execlp`, `execlpe`, `execvp`, `execvpe`); missing report is FAILED | `test_forgery.py::test_os_exit_forgery` |
| Grader forgery by writing a fake report | Static scan rejects strings containing `junit` or `report.xml`; exact test-ID manifest check | `test_forgery.py::test_fake_report_forgery` |
| Valid all-pass report plus `os._exit(0)` (otherwise PASSED) | Static scan (`_exit`, `report.xml`) turns the result into `TAMPER_SUSPECT` | `test_forgery.py::test_forged_report_with_os_exit_is_tamper` |
| Forged result envelope printed on stdout | The runner's own envelope is the last valid line and is produced by trusted code; the child's stdout is piped, not shared | `test_forgery.py::test_fake_envelope_on_stdout` (scan evaded), `test_forgery.py::test_fake_envelope_literal_is_flagged` (scan hit) |
| Valid all-pass report plus `os.execv("/bin/true", ...)` (clean exit 0 before pytest reports) | Static scan (`execv`, `report.xml`) turns the result into `TAMPER_SUSPECT` | `test_forgery.py::test_forged_report_with_execv_is_tamper` |
| Obfuscated forgery that evades the scan | Missing or incomplete report fails the run; manifest check | `test_forgery.py::test_obfuscated_exit_residual` |
| Hidden tests silently deselected | Test-ID manifest compared to the collected IDs | `tests/docker/test_sandbox_runner.py::test_missing_test_is_tamper` |

## Residual risks

- **Grading integrity against deliberately obfuscated forgery is best-effort.** The forgery scan is static and AST-based. A constructed `getattr` with a dynamically built attribute name, for example `getattr(__import__("o" + "s"), "_e" + "xit")(0)`, or a dynamically built report path, is not detected. Such runs usually still fail (no report is written, or the manifest does not match), but a patient adversary who both forges a complete valid report and exits cleanly through an obfuscated call could be graded PASSED. `test_obfuscated_exit_residual` documents the case that is caught only by the missing report.
- **Same-uid report forgery is detected only statically.** The candidate, pytest and `/work/report.xml` share uid 10001, so the candidate can write a report that looks exactly like pytest's. The runtime has no way to distinguish them. Only the static scan and the manifest check stand in the way.
- **Host containment is the hard guarantee.** Network isolation, a read-only rootfs, dropped capabilities, resource limits and no mounts do not depend on recognising any code pattern. A successful forgery can corrupt one grade. It cannot touch the host.
- **Codex's served model is recorded client-side.** The harness records the model it asked for and what the CLI reports; it cannot independently verify which model the vendor actually served.
- **The CLIs are trusted vendor binaries.** The harness trusts `claude` and `codex` to honour their sandbox and tool-disabling flags. The rollout tool audit detects violations after the fact but cannot prevent them.
- **Kernel and Docker escapes** are out of scope. They are mitigated by default seccomp and by keeping Docker current, not by this harness.
- **A syntax-error candidate is FAILED, not forgery.** `scan` records `syntax_error` in `info` only (R38): code that does not parse cannot run, so it cannot forge a report, and honest failures must not pollute tamper metrics.
- **`unscannable` stays forgery on purpose.** If the AST walk hits a `RecursionError`, the code parsed and therefore does execute, so it is treated as forgery rather than waved through.
- **Scan trade-offs (R39).** Calls to `exec`, `eval` and `compile` (bare, or via `builtins`/`__builtins__`) are forgery (`dynamic_exec`); honest dataset solutions do not need them, and they would otherwise defeat every other pattern. `kill`/`killpg` are forgery only when the base is the `os` module or an alias (`import os as o`, `from os import kill`), so `proc.kill()` and `self.kill()` stay clean; `_exit` is flagged on any base. `os` and `sys` import aliases are resolved. The `junit` string rule ignores docstrings and bare expression-statement strings, to avoid flagging honest documentation; a `report.xml` string is flagged everywhere. The cost: a solution that legitimately mentions `junit` in a real string literal, or calls `exec`/`eval`/`compile`, is graded `TAMPER_SUSPECT`.
- **Known scan evasions (best-effort grading).** These are not detected statically: a `report.xml` path built by string concatenation; `importlib.import_module` with a concatenated module name; a subprocess shell that writes the report (`subprocess.run("echo ... > /work/rep" + "ort.xml", shell=True)`); and `getattr` with a concatenated `_exit`. The missing or incomplete report and the manifest check still catch most of these, but not all.

- **Known static-scan evasions** (forgery detection is best-effort): `getattr(builtins, "exec")`, `__builtins__["exec"]`, `getattr` with a concatenated `_exit`, an alias assigned after import (`o = os`), `from sys import modules as m`, a concatenated `report.xml` path or `importlib.import_module("py"+"test")`, and a subprocess shell that writes the report. The candidate still has no network, no secrets, and no host access. These evasions affect only the honesty of the grade for a deliberately adversarial candidate, and the PRD's evaluation targets non-adversarial model output.
- **The scan reads the bytes the container runs (R40).** `scan` accepts `bytes | str` (a `str` is encoded as UTF-8, exactly as the tar builder does) and calls `ast.parse(source_bytes, feature_version=(3, 12))`, so a BOM or coding cookie is honoured the same way the container's CPython 3.12 does. `SandboxRunner.run` scans `files["solution.py"]` itself (flag `scan_candidate=True`), so the scanned bytes cannot differ from the executed bytes. There is no separate candidate string.
- **Clean-exit paths (R40).** A forged report passes only if pytest ends with exit 0 before writing its own report, or writes a report the candidate tampered with. Process replacement (`exec*` family, any base) is therefore forgery, as is frame and object walking that can reach pytest internals: `sys._getframe`, `inspect.currentframe`/`stack`/`trace`, `gc.get_objects`/`get_referrers`/`get_referents`. Other `inspect` uses stay info. A rare honest solution that uses frame introspection is labelled tamper.
- **Why `kill`/`killpg` stay narrowed to `os` (R39).** Any kill ends the container with a non-zero exit, and an all-pass report with a non-zero exit is already `TAMPER_SUSPECT` (Task 14), so a kill cannot yield PASSED. Flagging `proc.kill()` on arbitrary bases would only create false positives.
