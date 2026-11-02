# CLI profile evidence

1. Configured read-only and execution profiles require explicit host evidence. Native
   CLI presence, a model name or login state is insufficient. `requires-live-probe`
   and `unavailable` configurations never silently become supported.
2. The extracted command builders preserve the embedded source's explicit model/effort,
   permissions and child environment contracts. They have fake-CLI fixture coverage;
   this standalone candidate has no native version/install/invocation matrix.
3. The bundled `config/default.json` marks the Claude Code read-only and Codex
   read-only/execution profiles `supported` on the owner's attestation; it is not probe
   evidence. Replace it with an explicit `--config` where that attestation does not hold.
   No production execution policy is shipped.
   Claude Code execution is an orchestrator profile: the host policy lists `read`,
   `write-workspace` and `orchestrate`, with `delegation_blocked: true`; config is not
   isolated, because the child loads user, project and local settings (hooks, skills,
   instructions). It runs with `acceptEdits`, prompts denied, workspace tools, `Agent`
   and `Skill`, no MCP servers and no web tools. The launcher stays blocked inside it.
   `claude -p` reports neither model nor effort fields: the model counts as observed
   when the envelope's `modelUsage` lists the requested model, and a Claude Code resume
   pins session and model only, because effort is never reported. A trusted host may attest bounded local
   workspace actions only after checking isolation, delegation denial and fresh/resume
   behavior. Unknown combinations return `UNAVAILABLE`.
4. Native profile, actual provider identity, credits and billing require separate approved
   checks. Fake CLI tests prove process plumbing only; requested argv is not actual identity.
