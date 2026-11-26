# Copilot POC

Analyze an existing Antithesis run using your personal Copilot allowance.
The result is a report for review. No issues, PRs or paid runs are created.

## Set up once

1. In your personal GitHub settings, create a **fine-grained token** with your
   personal account as owner, public repository access, and the account permission
   **Copilot Requests**. No repository write permissions are needed.
2. In this repository's **Settings → Secrets and variables → Actions**, save it
   as **`COPILOT_POC_TOKEN`**. Never commit or share the token.

Copilot Free quotas apply. Check your allowance and spending settings before use.
[GitHub authentication guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli).

## Try it

Once the workflow is on `main`:

1. Open **Actions → Antithesis Copilot POC → Run workflow**.
2. Enter a completed **Antithesis run ID**, such as `45625243551`,
   not the evidence collector's run ID.
3. Download the `antithesis-copilot-poc-…` artifact and read **`analysis.md`**.
   Evidence, context, status and available usage statistics are included.

Or use:

```bash
gh workflow run antithesis-copilot-poc.yaml -f run_id=45625243551
```

## Keep in mind

- One tool-free response, a five-minute timeout, and no automatic retry.
  The timeout is not a billing cap.
- Conway and Leios tests stay unchanged.
- Missing logs, expired artifacts and truncated context limit the diagnosis.
  Source excerpts come from the tested commit; pinned image contents still
  require separate verification.
- Treat the analysis as a suggestion to investigate, not a confirmed fix.

Local checks, without an AI request:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_antithesis*.py'
actionlint .github/workflows/antithesis-copilot-poc.yaml
```
