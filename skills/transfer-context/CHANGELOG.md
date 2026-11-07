# Changelog

## 0.0.5 - 2026-11-07

1. Rename `context-transfer` to `transfer-context`; invoke `/transfer-context` (`$transfer-context` in Codex)
   and run `scripts/transfer_context.py`. The `context-transfer` package remains as a migration notice.
   The store `~/.sentimony/cross-agent/context-transfer/`, `CONTEXT_TRANSFER_DIR` and the
   `context-transfer:resume-check` marker keep their names.
2. Add `references/migration.md`: what changed, what stays, and the steps for installed hooks and links.

## 0.0.4 - 2026-11-07

1. Warn against pointing `--store` or `CONTEXT_TRANSFER_DIR` at a temporary
   directory such as `/tmp` (`20261104-0624`).

## 0.0.3 - 2026-11-05

1. Capture linked worktrees and local refs, and report drift and unverified facts.

## 0.0.2 - 2026-11-04

1. Document default context usage thresholds and overrides.

## 0.0.1 - 2026-11-04

1. Initial version.
