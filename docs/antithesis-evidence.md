# Accessible Antithesis evidence

This collector prepares evidence for later AI-assisted triage. It makes no
diagnosis and never opens issues, proposes code, or launches Antithesis runs.
No model credentials or Antithesis API key are needed.

## Automatic collection

1. The existing **Antithesis** workflow obtains its report URL as before.
   It keeps the raw Moog fact and resolved Compose configuration in private
   runner-temporary files. A best-effort step reads accessible report data
   and uploads `antithesis-report-evidence-<run-id>-<attempt>`.
2. When that workflow completes, **Collect Antithesis evidence** runs separately
   with only `actions: read` and `contents: read`. It checks the source workflow,
   repository, branch, attempt, and artifact provenance before reading the data.
   It executes collector code from the default branch, never downloaded code.
3. Its `antithesis-evidence-<collector-run-id>-<collector-attempt>` artifact
   contains `evidence.json` and `summary.md`, retained for 30 days so weekly
   runs remain available for later comparisons.

The report collection/upload steps cannot change the source test's conclusion.
If a job timeout prevents them from running, the follow-up still collects
GitHub context and records the missing report. The two workflows are restricted
to the existing governance workflow on `main`; Conway and Leios drivers,
images, genesis parameters, assertion requirements, and scheduling are unchanged.

The follow-up can also be dispatched manually with a **completed GitHub run ID**.
For older runs without a report artifact, it collects GitHub metadata and
diagnostic excerpts, and recovers explicit test metadata from supported runner
log entries. It cannot retrieve an old signed URL from the job
summary automatically. Use the local report command below when you have one.

## Bundle contents and limitations

- GitHub run ID, attempt, source SHA, URL, and workflow conclusion.
- Moog request ID, requested/tested commit, mode, Antithesis outcome and phase
  when obtained from the result fact. The patched test commit can differ from
  the workflow SHA; both are retained.
- Actual image references, era/protocol version and enabled feature flags,
  read from the source run's resolved Compose configuration when available.
- Matching property records, counts and up to five examples/counterexamples
  of each kind, including embedded moment/session/input-hash information.
- Bounded GitHub diagnostic excerpts and explicit evidence gaps.

When structured fields are missing, the collector can recover the selected
mode from the mode step's runner environment, the full commit supplied to
`create-test` after a successful request response, the request ID, and the
result step's actual `outcome:` output. It does not infer outcomes from exit
codes, parse historical Moog facts as current results, or use the workflow
head SHA as a substitute for the patched test commit. Each recovered field
has file/line provenance in `test.field_sources` and the Markdown summary.
Conflicting candidates stay unknown; structured artifact values take precedence
and disagreements are recorded. Outcome/commit candidates must belong to the
same request ID. Metadata recovery cannot manufacture missing property records.

The HTML report bundles historical runs. Properties must match the exact Moog
request ID, repository and tested commit; there is no "latest run" fallback.
Multiple matching platform run IDs are treated as ambiguous.

Report parsing uses the accessible bundle's **internal, undocumented format**.
It downloads at most the three newest chunks by default (configurable locally
up to eight), with per-file, download-total and request-time limits. It never
executes report JavaScript. This is always marked as a potentially incomplete
inventory, even if matching records were found. An unavailable report, changed
format, expired link or empty selection never becomes a passing verdict.

Raw `passed: false` records can include platform diagnostics, not just failed
test requirements. The collector preserves them without declaring root cause.
GitHub's workflow conclusion is separate from Antithesis's test outcome.

Only examples embedded in the report are available here. Artifacts requiring
sign-in or displaying **Contact Antithesis**, including some Tracer/execution
logs, are not requested. Their absence is recorded and must constrain any
future diagnosis.

New artifacts omit URL queries/fragments/userinfo and redact credential fields
and credential-bearing text. Raw report pages, raw Moog facts, raw log ZIPs and
wallet material are never uploaded. GitHub logs are reduced to diagnostic
excerpts rather than copied wholesale. Credential-bearing free text is dropped
conservatively, which can remove diagnostic context. The existing source workflow's report
link in its job summary is unchanged. Treat all collected logs as untrusted
data in any later AI workflow.

## Local replay

Use Python 3.11+ and curl. Keep the signed report URL in a private local file;
do not commit it or put it in a GitHub issue. A context JSON has this shape:

```json
{
  "github": {
    "repository": "input-output-hk/cardano-node-tests-antithesis",
    "run_id": "GITHUB_RUN_ID",
    "run_attempt": 1,
    "head_sha": "WORKFLOW_SOURCE_SHA"
  },
  "test": {
    "request_id": "MOOG_TEST_RUN_ID",
    "tested_commit": "ACTUAL_PATCHED_TEST_COMMIT_SHA",
    "mode": "leios"
  }
}
```

```bash
python3 scripts/collect-antithesis-evidence.py report \
  --context /tmp/run-context.json \
  --report-url-file /tmp/private-report-url \
  --output /tmp/report-evidence
```

For an offline report snapshot, add `--offline-report /tmp/report-snapshot`.
That directory contains `manifest.json` and `chunk<N>.bin` files. A report URL
is optional in offline mode. Outcome remains unknown without a Moog fact file;
an optional `--facts` file supplies the result fact, not a guessed verdict.

To assemble a complete local bundle from previously downloaded GitHub data:

```bash
python3 scripts/collect-antithesis-evidence.py finalize \
  --repository input-output-hk/cardano-node-tests-antithesis \
  --github-run-json /tmp/github-run.json \
  --github-logs-zip /tmp/github-logs.zip \
  --seed /tmp/report-evidence/evidence.json \
  --output /tmp/run-evidence
```

Without `--github-run-json` / `--github-logs-zip`, the `finalize` command can
retrieve a completed run with `--github-run-id <id>` and a `GH_TOKEN` with
Actions read access. Its token is sent only to the GitHub API, not redirected
storage hosts. Neither command obtains wallet secrets or launches a test.

Run the collector regressions independently of the node testnet:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_antithesis_evidence.py
```
