# Contributing

## Development checks

The runtime uses only the Python standard library. Run this complete local suite
against the final staged payload after a fresh cachebuster has been generated:

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONWARNINGS = "error::ResourceWarning"
python -B -X utf8 -m unittest discover -s tests -v
python -B -X utf8 .\scripts\release_gate.py validate --root .
python -B -X utf8 .\scripts\release_gate.py secrets --root . --history
python -B -X utf8 .\scripts\release_gate.py roundtrip --root .
python -B -X utf8 .\scripts\release_gate.py smoke --root .
```

The unit tests use local mock HTTP servers. They must not require a real API key or make requests to SiliconFlow.
The portable release gate is the CI contract and uses only the Python standard library plus Git. When the Codex system skills are available locally, also run their official `validate_plugin.py` and `quick_validate.py` validators; CI must not depend on a user-specific absolute path to those tools.
Release payloads are defined strictly by stage-zero Git index entries. Every intended payload file must be staged, use Git mode `100644`, remain a regular non-reparse file, and have byte-identical index/worktree content. Any non-ignored untracked file under the repository is reported as `PACKAGE_UNTRACKED_FILE`; stage every intended release file, or exclude local-only material, before the final gate run.
During an in-progress same-base release edit, `validate` deliberately reports
`VERSION_CACHEBUSTER_STALE` until the transactional cachebuster step. Run affected
unit suites, the secret scan, roundtrip, and smoke first; treat the complete block
above as the post-cachebuster release check.

## Repository boundaries

- Keep this Git repository as the authoritative source. Personal-marketplace source and installed `.codex/plugins/cache` trees are generated verification targets, not editing locations.
- Do not edit files under `.codex/plugins/cache`; reinstall the plugin to regenerate that cache.
- Do not commit `.plot-rag/`, SQLite databases, environment files, API keys, transcripts, or generated receipts.
- Keep third-party models proposal-only. Local validation and transactions remain the only writers of authoritative state.
- Treat `knowledge/plot_design_methods.json` as derived craft guidance, never as project facts.

## Updating the method catalog

When source guides change:

1. Re-read the affected files under `写作指南/功能模块整理/`.
2. Update only method cards whose behavior or boundary changed.
3. Refresh the corresponding SHA-256 values in `derived_from`.
4. Add or update differential retrieval tests for the affected task types.
5. Verify that injected guidance still defers to project facts and does not expose checklist narration in story output.

## Release flow

1. Add a release section to `CHANGELOG.md` and update the semantic base version in `.codex-plugin/plugin.json`, `scripts/plot_state.py`, `scripts/plot_rag_mcp.py`, and the `plot-rag-gate/<version> state-rag` User-Agent in `scripts/state_rag.py`.
2. Stage every intended release file. Run the affected unit suites, secret scan,
   roundtrip, and smoke; resolve every payload membership or index/worktree issue.
3. After source and documentation are final, generate a fresh Codex cachebuster through the transactional wrapper:

   ```powershell
   python -B -X utf8 .\scripts\release_gate.py cachebuster --root .
   ```

   The wrapper invokes the official plugin-creator helper, preserves the semantic base, validates the UTC token, normalizes `plugin.json` to LF, stages it, and runs validate/roundtrip/smoke. If any post-update gate fails, it restores both the manifest worktree bytes and its original Git index entry.
4. Run the complete development-check block above, then commit and push the source
   with the generated `+codex.<UTC>` version. The cachebuster-bearing manifest is
   part of the authoritative GitHub source; future releases replace the suffix
   through the same wrapper.
5. Reinstall from the personal marketplace and verify the running surface:

   ```powershell
   codex plugin add plot-rag-gate@personal --json
   codex plugin list --json
   codex mcp list --json
   ```

6. Confirm marketplace resolution and installed-cache bytes match the committed repository source:

   ```powershell
   python -B -X utf8 .\scripts\release_gate.py verify-install `
     --source . `
     --marketplace <MARKETPLACE_JSON> `
     --installed <INSTALLED_PLUGIN_CACHE>
   ```

7. Only after the comparison reports `PASS`, create and push the semantic version tag (for example, `v1.4.3`) at that exact cachebuster-bearing commit. The Git tag uses the semantic base version while GitHub source, marketplace source, and installed cache keep identical bytes.
