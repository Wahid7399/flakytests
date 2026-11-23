# Flaky-test study: openai-agents-python & semantic-kernel

## Projects / commits
| Project | Repo | Commit | Test root |
|---|---|---|---|
| 1 | https://github.com/openai/openai-agents-python | `e23c6ffb0fe40cbae3c8d832940d9459571e75da` | `tests` |
| 2 | https://github.com/microsoft/semantic-kernel (Python package, `python/`) | `58ad1a3780159300c766477f258a4cdfa768eedf` | `tests/unit` (minus `tests/unit/processes/dapr_runtime`) |

Cloned into `projects/` (not committed). Only the Python test suites were used.
Semantic-kernel scope: unit tests only. `tests/integration` and `tests/samples` need live cloud services/API keys. The 8 `dapr_runtime` modules fail at collection because the `dapr` package is not available in the locked environment; they are excluded.

## Environment
- Linux x86_64 (GitHub Codespace), 4 visible cores, ~8 GB RAM; see `results/env/system.txt`.
- Python 3.12.3, managed with `uv` using each project's own `uv.lock`:
  - Project 1: `cd projects/openai-agents-python && uv sync --python 3.12 --all-extras --group dev`
  - Project 2: `cd projects/semantic-kernel/python && uv sync --python 3.12 --frozen --extra anthropic --extra autogen --extra aws --extra azure --extra chroma --extra copilotstudio --extra faiss --extra google --extra hugging_face --extra mcp --extra milvus --extra mistralai --extra mongo --extra ollama --extra onnx --extra oracledb --extra pandas --extra pinecone --extra postgres --extra qdrant --extra redis --extra realtime --extra sql --extra usearch --extra weaviate`
- Exact installed packages: `results/env/*.requirements.freeze.txt`.
- No API keys or network services are provided; tests run as shipped (`PYTHONHASHSEED=0`, no random-order or xdist plugins).

## Harness (`harness/`)
- `flaky_plugin.py` – pytest plugin. Outcome per test: `1` pass, `0` failed (assertion, error in setup/call/teardown), `S` skipped (incl. xfail), `N` no result (crash/hard timeout/not collected).
- `study.py` – driver (`collect`, `suite`, `isolation`, `aggregate`). `aggregate.py` builds the CSVs. `run_all.sh` is the campaign actually executed.
- **Collection**: `pytest --collect-only`; test_id = full pytest node id including parameters; `test_name` = node id without the file path; `module` = file path.
- **Setting B (suite)**: one fresh `python -m pytest <root>` process per run, single process, default test order. Per-test results come from the pytest reports. Per-run logs: `results/<p>/logs/suite_run_<k>.log`; failure tracebacks: `raw/suite_run_<k>.failures.log`; durations: `raw/suite_run_<k>.json`; wall time/exit code: `suite_runs.jsonl`.
- **Setting A (isolation)**: pytest collects once, then for each (test, repetition) the process `fork()`s and the child executes only that test (setup, call, teardown; no other test has run before it), reporting back through a pipe; the child is killed after a 300 s hard timeout (`N`). Forking avoids re-importing the project for every one of the 11,865/4,120 tests; each repetition still starts from a pristine post-collection interpreter copy. This is a deliberate deviation from "a new `pytest <test_id>` process" and a threat to validity. Results/tracebacks: `raw/iso_shard*.jsonl`.
- Reproduce: `python3 harness/study.py collect <project>`, then `suite`/`isolation`/`aggregate` (see `harness/run_all.sh`).

## IMPORTANT: scale actually executed
The requested scale (5,000 full-suite runs and 5,000 isolated runs of every test) is not feasible here. Measured cost:

| | cost per unit | spec total |
|---|---|---|
| openai-agents full suite (11,865 tests) | ~820 s | ~47 days |
| semantic-kernel full suite (4,120 tests) | ~96 s | ~5.5 days |
| isolated test execution | ~0.1 s | 59M runs (~70 days) + 20.6M runs (~24 days) |

I therefore ran a reduced number of repetitions with the identical pipeline (exact numbers in the Results section). CSVs contain exactly the executed columns `run_1..run_K`; `python3 harness/study.py aggregate <project> --pad-to 5000` pads to 5,000 columns with `N`, but statistics are computed over executed runs only. Observed flake counts from few repetitions are lower bounds: a test failing with probability p per run is detected with probability 1-(1-p)^K, so low-rate flaky tests will be missed. Extending toward 5,000 only needs `suite --start`/`isolation --start` (isolation is resumable and append-only).

## Outputs (`results/<project>/`)
- `tests.csv` – `test_id,test_name,module,sha`
- `isolation_results.csv`, `suite_results.csv` – `test_id,test_name,module,sha,run_1..run_K`
- `isolation_failure_summary.csv`, `suite_failure_summary.csv` – `test_name,fail_runs_count,first_failing_run,all_failing_runs` (all tests; `test_name` is the full test id; failing fields empty if none), derived from the results CSVs
- `*_stats.csv` – pass/fail/skip/missing counts, failure rate (fail / (pass+fail)), class (flaky = ≥1 pass and ≥1 fail)
- `comparison.csv`, `summary.json` – flaky in both / only isolation / only suite / consistent
- `isolation_failure_details.jsonl`, `logs/`, `raw/` – error messages, stack traces, timings

## Results (reduced scale — NOT 5,000 runs)
Executed (see `results/*/summary.json`, `suite_runs.jsonl`, `results/campaign*.log`):

| Project | tests | suite runs | isolation reps/test |
|---|---|---|---|
| openai-agents-python | 11,865 | 5 | 5 |
| semantic-kernel (unit) | 4,120 | 20 | 10 |

| Category | openai-agents-python | semantic-kernel |
|---|---|---|
| flaky in both settings | 0 | 0 |
| flaky only in isolation | 0 | 0 |
| flaky only in full suite | 0 | 0 |
| consistently pass | 11,827 | 4,114 |
| consistently skipped | 35 (+1 skipped in isolation but passing in suite) | 6 |
| consistently fail (both settings) | 2 (environmental) | 0 |

**Consistent failures (environmental, not flaky):** `test_runner_preserves_unix_local_lifecycle_state_across_pause_and_resume` (the `rclone` mount tool is not installed) and `test_regular_write_preserves_inode_permissions_and_umask` (file mode/umask expectation does not hold in this Codespace). Tracebacks: `raw/suite_run_*.failures.log`, `isolation_failure_details.jsonl`.

**Re-run note.** The first openai-agents campaign was started from a background shell that ignored SIGINT and inherited an injected `GIT_CONFIG_*` setting `safe.bareRepository=explicit`. That made 4 tests fail for environmental reasons (`test_docs_publish_static_artifact_to_existing_branch` and three `[2]` cancellation tests in `test_code_change_verification_runner.py`). `harness/study.py` now resets SIGINT and overrides that git setting, and all openai-agents suite runs and the isolation runs of those 4 tests were re-run. The first-attempt data is not used in any result above (kept locally in `results/archive_first_attempt_oa/`, not part of the delivered archive).

**Interpretation:** with 5–20 repetitions only frequent flakiness would be detected. No flaky test was observed, which is not evidence that none exist at the 5,000-run scale. The campaign process was killed and resumed several times (all steps resume from existing files); `results/campaign*.log` contain the partial attempts.
