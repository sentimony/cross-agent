# Registry and installation-root migration

1. The trusted host supplies an absolute private `task_context.registry_path` outside
   plugin installation roots. Changing a plugin/cache root does not choose a new registry,
   task ID, host session ID, provider session ID or artifact root automatically.
2. Reuse the same canonical repository/issue and conversation context after relocation.
   Generic schema1 task/session records remain valid without optional owner integration
   metadata. Preserve provider provenance, conflicts/quarantine and previous artifact paths.
3. For a generic compatible session, the relocated launcher can select the same actual
   provider session. An installed owner's contract decides whether older domain review
   artifacts are compatible; generic schema1 never grants domain validation automatically.
4. Unknown/corrupt legacy schemas return explicit `CORRUPT_REGISTRY`/blocked evidence.
   Keep the original bytes; do not reset, delete, copy credentials or guess a conversion.
   Unsupported source paths are not a reason to silently start a new provider session.
5. `test_state_relocation.py` copies only the public state module into two fixture
   installation roots and proves stable task/session/provider/artifact identity against one
   private registry. It also proves unknown legacy data is retained without reset.
6. This fixture contract does not prove native host cache migration, real provider resume,
   released version compatibility or safe production cleanup. Those need separate evidence.
