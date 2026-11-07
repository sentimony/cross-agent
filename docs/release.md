# Release cross-agent

1. Keep the initial plugin and skill at 0.0.1. Later changes increase the patch only.
   Match the version in the root `plugin.json` (the full manifest; hidden manifests
   exist only for hosts listed in `KEPT_HIDDEN` of `scripts/test_package.py`), skill
   metadata, `README.md`, and the newest dated changelogs.
2. Fix the exact source commit/tree, owned changes, intended base and current user
   authority. Preserve all unrelated work and never overwrite an existing tag.
3. Run python3 -B scripts/run_tests.py. Both suites must collect tests; its private
   HOME/PATH excludes native agent CLIs and inherited provider credentials. Run the
   actual CI validation, whitespace, metadata and relative-resource checks.
4. Obtain independent read-only readiness approval with the exact final tree. This
   repository is outside the registered agentflow release roles; the authorized
   parent orchestrator performs publication with an independent verifier.
5. For the first empty remote only, create a minimal empty main bootstrap and merge
   that bootstrap into the reviewed dated feature branch. Keep all implementation
   changes in the feature PR. Require CI, squash merge and pin the exact merged SHA.
6. Publish an annotated vX.Y.Z tag (the version from item 1) on the squash-merged main
   commit, then create a GitHub release. Verify remote tag/commit, release, the root
   manifest and any kept hidden manifest, and installed resource paths.
   Never force-push, move a published tag or copy credentials into scratch configs.
7. Update both marketplace catalogs only after the standalone tag and compatible
   agentflow owner tag exist. Record exact refs and protocol compatibility.
8. Keep native installation, provider sessions and live end-to-end behavior explicitly
   unverified until actual evidence exists. Accepted initial-release risks are tracked
   in https://github.com/sentimony/agentic-workflow/issues/31. Release approval does
   not establish native support or a successful production run.
