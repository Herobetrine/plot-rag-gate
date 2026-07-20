# Long-form and power-system benchmark fixtures

This directory contains deterministic, offline, standard-library-only fixtures
for the long-form continuity, craft-memory, and typed power-system engines.

- `fixtures/longform_annotations.v1.jsonl`: 240 versioned end-to-end cases.
  Evaluation begins with `assistant_text`, parses `<plot-delta>` proposal
  blocks, resolves canonical names and aliases, validates exact evidence spans
  and SHA-256 hashes, normalizes the event through
  `continuity.validators.normalize_event`, applies semantic invariants, and
  finally scores accepted, quarantined, or zero-delta outcomes.
- `fixtures/chapters_500.v1.jsonl`: a compact 500-chapter corpus used to verify
  persistent indexing, incremental SHA-256 refresh behavior, and bounded
  context assembly.
- `fixtures/power_system_annotations.v1.jsonl`: 360 versioned end-to-end power
  cases spanning all 12 adapters. It covers typed Stop envelopes, accepted,
  dangerous, and zero-delta outcomes, runtime invariants, read-only tri-state
  queries, cross-system isolation, and normalized CLI/MCP projections.
- `fixtures/remote_responses.v1.json`: redacted offline response envelopes for
  embedding, rerank, authoritative chat JSON, and tool/function shadow
  decoding. Every protocol has one accepted and one malformed fixture replayed
  through the production decoder in strict CI without network access.

The annotation corpus has four balanced continuity categories:

- location
- inventory
- story time
- relationship

Every category contains 40 accepted deltas, 10 dangerous deltas, and 10
zero-delta outputs. Dangerous candidates contain no precomputed
`quarantined`/`dangerous_delta` flag. Thirty of the forty dangerous cases are
rejected by the production continuity normalizer; the remaining self-relation
cases pass schema normalization and are rejected by the semantic gate.
Entity-resolution labels cover canonical mentions and aliases independently
of the proposal outcome labels.

Regenerate the checked-in fixtures:

```powershell
python benchmarks/generate_fixtures.py
python benchmarks/generate_power_fixtures.py
```

Validate or run the annotation benchmark:

```powershell
python benchmarks/run_longform_benchmark.py validate
python benchmarks/run_longform_benchmark.py run

python benchmarks/run_longform_benchmark.py validate `
  --manifest benchmarks/fixtures/power_system_annotations.v1.jsonl
python benchmarks/run_longform_benchmark.py run `
  --manifest benchmarks/fixtures/power_system_annotations.v1.jsonl

python scripts/plot_state.py longform benchmark `
  --manifest benchmarks/fixtures/power_system_annotations.v1.jsonl
```

The run output is stable JSON and includes:

- accepted-delta `tp`, `fp`, `fn`, precision, recall, and per-category recall;
- `zero`, zero-delta accuracy, quarantine precision/recall, validator stage and
  reason counts;
- entity-resolution and alias-resolution accuracy;
- validator invocation counts, proposal candidate count, and normalized corpus
  SHA-256.

The power result additionally reports:

- profile-contract coverage for all 12 adapters;
- accepted, dangerous, and zero-delta counts;
- cross-system dangerous-case blocking;
- runtime-invariant and read-only tri-state checks;
- a normalized projection SHA-256 used to compare CLI and MCP results.

The benchmark reads only the checked-in JSONL file and holds all intermediate
proposals in memory. It never creates or mutates a user novel project.

## v1.5 retrieval performance harness

`v15_performance_manifest.v1.json` is a deterministic, project-neutral fixture
for the v1.5 retrieval execution path. The runner uses independent production
`AuthorityIndex` instances with offline embedding and rerank providers. It
covers:

- 1, 3, and 5 independent retrieval needs;
- strict legacy serial versus batched-path selected-chunk and context hashes;
- cold and hot candidate-cache phases;
- one batch embedding request per cold healthy scenario;
- exception, wrong-length, bad-index, and duplicate-index batch fallback;
- one degraded need retaining BM25/FTS5/lexical candidates;
- bounded parallel rerank, including a 5-need scenario capped at 2 workers;
- warmup and measured iterations with p50/p95 stage timings;
- git/plugin/Python/platform/CPU and effective-parameter provenance hashes;
- redacted stdout and artifacts containing hashes rather than prose, prompts,
  project paths, environment values, credentials, or free-form project IDs.

Regenerate and validate the fixture:

```powershell
python benchmarks/generate_v15_performance_fixtures.py
python benchmarks/run_v15_performance_benchmark.py validate
```

Run the offline benchmark. By default it creates a unique
`.plot-rag-benchmark/<UTC timestamp>-<run id>/` directory and refuses to
replace an existing artifact. When explicit paths are supplied, both output
paths are preflighted before either file is written, so a pre-existing result
or manifest fails closed without leaving a half-written artifact pair:

```powershell
python benchmarks/run_v15_performance_benchmark.py run `
  --iterations 5 `
  --warmup-iterations 1
```

Use `--overwrite` to explicitly replace both artifacts at their requested
paths. `--output` and `--redacted-manifest-output` must always resolve to
different files:

```powershell
python benchmarks/run_v15_performance_benchmark.py run `
  --output .plot-rag-benchmark/result.redacted.json `
  --redacted-manifest-output .plot-rag-benchmark/run-manifest.redacted.json `
  --overwrite
```

Use `--rerank-delay-ms 0` for a fast contract-only run. The default synthetic
delay makes concurrent rerank overlap visible and enables the severe-regression
timing gate. The temporary project and independent SQLite indexes are deleted
after every run, including when `--workspace-parent` is supplied.

## v1.5 isolated novel-project E2E harness

`run_v15_live_e2e.py` exercises the FF/FT/TF/TT Prepare rollout matrix against
at least 25 structured novel prompts and, by default, runs the strict
Grill -> event-experience -> Prepare -> deterministic typed events -> proposal
review -> accept -> replay -> next-Prepare continuity chain. The strict chain
does not call remote Chat; it isolates lifecycle correctness from model
variance. The harness always executes against temporary copies; the source
project is protected by content, metadata, path, mtime, count, and byte-size
snapshots.

Run the read-only preflight first. `validate` performs no project copy, creates
no workspace, writes no report, and makes no remote request:

```powershell
python -B -X utf8 benchmarks/run_v15_live_e2e.py validate `
  --project-root "C:\path\to\novel"
```

Run the deterministic offline matrix and strict chain:

```powershell
python -B -X utf8 benchmarks/run_v15_live_e2e.py offline `
  --project-root "C:\path\to\novel" `
  --pretty
```

Run the same matrix with SiliconFlow Embedding and Rerank. Add
`--chat-extraction-smoke` to perform one separate real SiliconFlow Chat
extraction without writing continuity state:

```powershell
$env:SILICONFLOW_API_KEY='<TOKEN>'
python -B -X utf8 benchmarks/run_v15_live_e2e.py live `
  --project-root "C:\path\to\novel" `
  --chat-extraction-smoke `
  --pretty
```

The redacted result contains timings, call counts, model names, hashes, quality
gate counts, and formal-project snapshot comparisons. It excludes prompt and
novel prose, raw remote responses, credentials, and the source-project absolute
path. `--workspace-parent` and `--output` must be outside the source project;
`validate` rejects all workspace and output flags.

Latency surfaces are intentionally separate: Prepare reports local orchestration
plus Embedding/Rerank time, the strict chain reports deterministic lifecycle
time, and Chat smoke reports wall, remote, and local-overhead time independently.
