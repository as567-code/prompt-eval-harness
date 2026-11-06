# Security Policy

## Scope

- Sandbox containment: untrusted, model-generated code runs only inside the Docker sandbox.
- $0 CLI-only execution: LLMs are called only through the `claude` and `codex` CLIs.
- No API keys are ever used. The harness never reads, stores, or forwards API keys, and child
  processes run with a scrubbed environment. This guarantee covers environment variables only.
  Credentials stored by the CLIs themselves (`~/.claude` settings or keychain,
  `~/.codex/auth.json`) are checked separately at run time (Phase 1).

## Reporting a problem

Open a private security advisory on this repository (GitHub Security tab, "Report a vulnerability").
Please do not open a public issue for security problems.
