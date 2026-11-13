# Provisional extraction map

1. Embedded source: `sentimony/agentic-workflow` commit
   `a37b6e47a8ba388b28870cac80d90f4d8e3fb113`, tree
   `2dedd3ef82ce72f631e3754f738f86cde642170e`, MIT.
2. Prospective local agentflow candidate: HEAD/base
   `79b84cc5e0ba1d855c7f8ab0141d8ef25a9af395`, tree
   `312d0675e31b77552a6cf599d98619ed3e1cd284`, provisional package `0.0.13`.
   This is an actual local preparation source, not verified G1 delivery.
3. `cross_agent_request.py` and `cross_agent_state.py` transfer generic parser/policy and
   registry/session/provenance logic; provider ownership and quarantine data are retained.
4. `cross_review.py` generic dependency closure becomes `cli_transport.py`: explicit profiles,
   environment/loop guard, command builders, actual identity, provider/usage parsers and
   private artifact helpers. The source contains profile definitions; `cli_profiles.py`
   did not exist. Review target recipes, briefs, archive and intake are excluded.
5. `cross_agent.py` retains generic process/provider/session dispatch. Workflow-specific
   `standard_sections`, `validate_review`, `select_review_intake` and `_fresh_target`
   are removed. The owner supplies preparation/validation/intake through protocol1.
6. Original generic request/state tests and process fixture cases transfer. Workflow review
   tests remain owner-owned. New protocol/resource tests cover the extracted boundary.
   Real signal and timeout probes are not included in the local fake-only verification.
7. No source installation, release, remote repository or live compatibility pair is claimed.
