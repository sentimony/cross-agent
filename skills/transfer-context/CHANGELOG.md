# Changelog

## 0.0.8 - 2026-11-12

1. `resume-check` compares a checkpoint with the worktree where it was written when it runs
   from another worktree of the same repository. The result has `resumed_from_other_worktree`;
   the change of place is not drift. A removed worktree gives one `worktree-removed` entry
   (`20261110-2223`).

## 0.0.7 - 2026-11-10

1. Warn through `store_warning` in `facts --json` when the store lies under a temporary
   directory (`20261104-0624`).
2. Lighten the surface (`20261106-1735`): `argument-hint` is `[save | resume [<path|id>] | check]`;
   `write --auto` completes a draft with four sections; `write` prints
   `resume_command_terminal` and `resume_command_terminal_codex`; the usage check moves to
   `references/usage-check.md`; `list`, `validate` and `prune` are described in the
   checkpoint format reference.

## 0.0.6 - 2026-11-10

1. Add `references/migration.md`: what changed in the rename from `context-transfer`, what
   stays, and the steps for installed hooks and links.

## 0.0.5 - 2026-11-07

1. Rename `context-transfer` to `transfer-context`; invoke `/transfer-context` (`$transfer-context` in Codex)
   and run `scripts/transfer_context.py`. Only the name and its mentions changed. The store
   `~/.sentimony/cross-agent/context-transfer/`, `CONTEXT_TRANSFER_DIR` and the
   `context-transfer:resume-check` marker keep their names.

## 0.0.4 - 2026-11-07

1. Warn against pointing `--store` or `CONTEXT_TRANSFER_DIR` at a temporary
   directory such as `/tmp` (`20261104-0624`).

## 0.0.3 - 2026-11-05

1. Capture linked worktrees and local refs, and report drift and unverified facts.

## 0.0.2 - 2026-11-04

1. Document default context usage thresholds and overrides.

## 0.0.1 - 2026-11-04

1. Initial version.
