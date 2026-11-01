# CLI profile evidence

1. Configured read-only and execution profiles require explicit host evidence. Native
   CLI presence, a model name or login state is insufficient. `requires-live-probe`
   and `unavailable` configurations never silently become supported.
2. The extracted command builders preserve the embedded source's explicit model/effort,
   permissions and child environment contracts. They have fake-CLI fixture coverage;
   this standalone candidate has no native version/install/invocation matrix.
3. No production execution policy is shipped. A trusted host may attest bounded local
   workspace actions only after checking isolation, delegation denial and fresh/resume
   behavior. Unknown combinations return `UNAVAILABLE`.
4. Native profile, actual provider identity, credits and billing require separate approved
   checks. Fake CLI tests prove process plumbing only; requested argv is not actual identity.
