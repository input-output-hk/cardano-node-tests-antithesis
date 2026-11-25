#!/usr/bin/env python3
"""Collect accessible evidence; never diagnose, change tests, or launch runs.

Report extraction is deliberately best-effort: Antithesis's bundled HTML/JS
format is not a public API. Missing or changed data remains unknown.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import pathlib
import re
import subprocess
import tempfile
import urllib.parse
import zipfile
from html.parser import HTMLParser

MIB = 1024 * 1024
MAX_FILE = 32 * MIB
MAX_TOTAL = 64 * MIB
WORKFLOW = ".github/workflows/cardano-node-governance.yaml"
URL = re.compile(r"https?://[^\s<>\"'`]+")
SENSITIVE = re.compile(
    r"password|passphrase|mnemonic|authorization|(^|_)auth($|_)|authentication|auth.?header|private.?key|signing.?key|(^|_)skey|token|secret|wallet|cookie|credential|api.?key|access.?key",
    re.I,
)
ASSIGNMENT = re.compile(r"""(?<![A-Za-z0-9_.-])([A-Za-z_][A-Za-z0-9_.-]*)["']?\s*[:=]""")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class EvidenceError(Exception):
    """Messages are controlled strings, never exceptions containing signed URLs."""


def clean_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        # Userinfo, all query parameters, and fragments are intentionally removed.
        return urllib.parse.urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
    except ValueError:
        return "[invalid URL redacted]"


def clean(value, depth=0):
    if depth > 20:
        return "[depth limit]"
    if isinstance(value, dict):
        if re.search(r"signing.?key", str(value.get("type", "")), re.I):
            return "[redacted signing-key envelope]"
        return {
            str(k): "[redacted]" if SENSITIVE.search(str(k)) else clean(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [clean(v, depth + 1) for v in value[:4096]]
    if isinstance(value, str):
        value = ANSI.sub("", value)
        value = URL.sub(lambda m: clean_url(m.group()), value)
        value = re.sub(
            r"(?i)((?:authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+", r"\1[redacted]", value
        )
        value = re.sub(r"(?i)(Bearer|Basic)\s+[^\s\"']+", r"\1 [redacted]", value)
        # Use the SAME sensitive-field vocabulary for stderr/embedded JSON.
        # Drop the whole text when it contains a credential assignment: a
        # partial regex replacement can leave nested/escaped values behind.
        probe = value.replace("\\", "")
        if any(SENSITIVE.search(m.group(1)) for m in ASSIGNMENT.finditer(probe)):
            return "[redacted credential-bearing text]"
        if re.search(r"signing.?key", probe, re.I) and re.search(r"cbor.?hex", probe, re.I):
            return "[redacted signing-key envelope]"
        return value[:16000]
    return value


def read(path: pathlib.Path, limit=MAX_FILE) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise EvidenceError("File exceeded the evidence size limit")
    return data


def load(path: pathlib.Path):
    return json.loads(read(path))


class Downloader:
    def __init__(self):
        self.used = 0

    def get(self, url: str, limit=MAX_FILE, token=None, github_redirect=False) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise EvidenceError("Only HTTPS download URLs without userinfo are accepted")
        if token and parsed.hostname != "api.github.com":
            raise EvidenceError("GitHub credentials can only be sent to api.github.com")
        if self.used >= MAX_TOTAL:
            raise EvidenceError("Total evidence download limit reached")
        limit = min(limit, MAX_TOTAL - self.used)
        with tempfile.TemporaryDirectory(prefix="antithesis-download-") as directory:
            root = pathlib.Path(directory)
            args = [
                "curl",
                "-q",
                "--globoff",
                "--silent",
                "--compressed",
                "--proto",
                "=https",
                "--connect-timeout",
                "10",
                "--max-time",
                "40",
                "--max-filesize",
                str(limit),
                "--output",
                str(root / "body"),
                "--write-out",
                "%{http_code}\n%{redirect_url}",
            ]
            if token:
                header = root / "header"
                header.write_text(
                    f"Authorization: Bearer {token}\nAccept: application/vnd.github+json\n"
                )
                header.chmod(0o600)
                args += ["--header", "@" + str(header)]
            # No wallet/GitHub environment or ~/.curlrc is inherited by curl.
            try:
                result = subprocess.run(
                    [*args, url],
                    capture_output=True,
                    timeout=45,
                    check=False,
                    env={"PATH": os.environ.get("PATH", os.defpath)},
                )
            except (OSError, subprocess.TimeoutExpired):
                raise EvidenceError("Download unavailable or timed out") from None
            status, _, redirect = result.stdout.decode(errors="replace").partition("\n")
            if result.returncode:
                raise EvidenceError(
                    f"Download failed (HTTP {status if status.isdigit() else 'unknown'}, curl {result.returncode})"
                )
            if status in ("301", "302", "303", "307", "308") and github_redirect:
                host = urllib.parse.urlsplit(redirect).hostname or ""
                if not host.endswith(
                    (".githubusercontent.com", ".blob.core.windows.net", ".github.com")
                ):
                    raise EvidenceError("GitHub returned an unsupported download host")
                # Signed object-storage redirects receive NO GitHub token.
                return self.get(redirect, limit=limit)
            if status != "200":
                raise EvidenceError(f"Download unavailable (HTTP {status})")
            data = read(root / "body", limit)
            self.used += len(data)
            return data


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            src = dict(attrs).get("src")
            if src:
                self.sources.append(src)


def report_asset(base: str, asset: str) -> str:
    url = urllib.parse.urljoin(base, asset)
    if urllib.parse.urlsplit(url).hostname != urllib.parse.urlsplit(base).hostname:
        raise EvidenceError("Report asset is not on the report's own host")
    return url


def manifest(text: str) -> dict:
    start = text.find("{")
    if start < 0:
        raise EvidenceError("Unsupported report manifest")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        raise EvidenceError("Unsupported report manifest") from None
    if not isinstance(value, dict) or not isinstance(value.get("chunk_id_to_chunk_location"), dict):
        raise EvidenceError("Unsupported report manifest")
    return value


def properties(data: bytes):
    """Read embedded JSON values only; never execute report JavaScript."""

    def walk(value, depth=0):
        if depth > 16:
            return
        if isinstance(value, dict):
            if {"property_id", "group_id", "passed", "run_id", "description"} <= value.keys():
                if type(value["passed"]) is bool:
                    yield value
                return
            for child in value.values():
                yield from walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child, depth + 1)
        elif isinstance(value, str) and value[:1] in ("{", "["):
            try:
                parsed = json.loads(value)
            except ValueError:
                return
            yield from walk(parsed, depth + 1)

    text = data.decode("utf-8", errors="replace")
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{"body":', text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        yield from walk(value)


def select_properties(records, context):
    expected = context["test"]
    request_id = expected.get("request_id")
    repository = context["github"]["repository"].lower()
    if not request_id:
        raise EvidenceError(
            "No Moog request identity; historical properties cannot safely be selected"
        )
    selected = {}
    for item in records:
        try:
            description = json.loads(item["description"])
            test = description["testRun"]
            repo = test["repository"]
            if description["testRunId"] != request_id:
                continue
            if f"{repo['organization']}/{repo['repo']}".lower() != repository:
                continue
            if expected.get("tested_commit") and test["commitId"] != expected["tested_commit"]:
                continue
        except (ValueError, KeyError, TypeError):
            continue
        key = item["run_id"], item["property_id"], item["group_id"]
        if key not in selected or item.get("insert_time", "") > selected[key].get(
            "insert_time", ""
        ):
            selected[key] = item
    if len({key[0] for key in selected}) > 1:
        raise EvidenceError("Multiple platform runs match this request; refusing to combine them")
    result = []
    for item in selected.values():
        record = {
            k: item.get(k)
            for k in (
                "run_id",
                "property_id",
                "group_id",
                "passed",
                "example_count",
                "counterexample_count",
            )
        }
        for key in ("examples", "counterexamples"):
            samples = []
            for sample in (item.get(key) or [])[:5]:
                if isinstance(sample, str):
                    try:
                        sample = json.loads(sample)
                    except ValueError:
                        pass
                samples.append(clean(sample))
            record[key] = samples
        result.append(clean(record))
    return sorted(result, key=lambda x: (x["passed"], x["group_id"], x["property_id"]))


def context_from_env():
    return {
        "github": {
            "repository": os.environ.get("GITHUB_REPOSITORY", ""),
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "run_attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
            "head_sha": os.environ.get("GITHUB_SHA", ""),
        },
        "test": {
            "request_id": os.environ.get("EVIDENCE_REQUEST_ID", ""),
            "tested_commit": (
                os.environ.get("EVIDENCE_TESTED_COMMIT", "")
                if os.environ.get("EVIDENCE_REQUEST_ID")
                else ""
            ),
            "mode": os.environ.get("EVIDENCE_MODE", "unknown"),
        },
    }


def collect_report(args):
    context = load(args.context) if args.context else context_from_env()
    bundle = {"schema_version": 1, **context, "properties": [], "gaps": []}
    bundle["limits"] = {
        "report_chunks": args.max_chunks,
        "samples_per_property": 5,
        "text_characters": 16000,
    }
    bundle["report"] = {"status": "unavailable", "inventory_complete": False}
    bundle["execution_logs"] = {
        "status": "not_collected",
        "reason": "Only report-embedded examples are collected. Restricted execution/Tracer artifacts are not requested.",
    }
    url = ""
    try:
        if args.facts and args.facts.exists():
            facts = load(args.facts)
            facts = facts if isinstance(facts, list) else [facts]
            if (
                len(facts) != 1
                or not isinstance(facts[0], dict)
                or not isinstance(facts[0].get("value"), dict)
            ):
                raise EvidenceError("Missing or ambiguous Moog result fact")
            fact = facts[0]["value"]
            if fact.get("testRunId") and fact["testRunId"] != bundle["test"].get("request_id"):
                raise EvidenceError("Moog result fact does not match the requested test run")
            bundle["test"].update(
                {"outcome": fact.get("outcome", "unknown"), "phase": fact.get("phase", "unknown")}
            )
            url = fact.get("url") or ""
        if args.report_url_file:
            url = read(args.report_url_file, MIB).decode().strip()
        if url:
            host = urllib.parse.urlsplit(url).hostname or ""
            if not host.endswith(".antithesis.com"):
                raise EvidenceError("Report URL is not an Antithesis tenant URL")
            bundle["report"]["url"] = clean_url(url)
        if not bundle["test"].get("request_id"):
            raise EvidenceError(
                "No Moog request identity; historical properties cannot safely be selected"
            )
        downloader = Downloader()
        if args.offline_report:
            m = load(args.offline_report / "manifest.json")
        else:
            if not url:
                raise EvidenceError("No accessible report URL was obtained by the source workflow")
            page = Scripts()
            page.feed(downloader.get(url, MIB).decode())
            m = None
            for src in page.sources[:4]:
                try:
                    m = manifest(downloader.get(report_asset(url, src), MIB).decode())
                    break
                except (EvidenceError, UnicodeError):
                    continue
            if m is None:
                raise EvidenceError("Accessible page did not contain a supported report manifest")
        locations = m.get("chunk_id_to_chunk_location")
        if (
            not isinstance(locations, dict)
            or not locations
            or not all(str(k).isdigit() for k in locations)
        ):
            raise EvidenceError("Unsupported report chunk map")
        chunk_ids = sorted(locations, key=int, reverse=True)[: args.max_chunks]
        found = []
        scanned = 0
        for chunk_id in chunk_ids:
            try:
                data = (
                    read(args.offline_report / f"chunk{chunk_id}.bin")
                    if args.offline_report
                    else downloader.get(report_asset(url, locations[chunk_id]))
                )
                found.extend(properties(data))
                scanned += 1
            except EvidenceError as exc:
                bundle["gaps"].append(f"Report chunk {chunk_id}: {exc}")
        bundle["properties"] = select_properties(found, context)
        bundle["report"].update(
            {
                "status": "extracted_partial"
                if bundle["properties"]
                else "unsupported_or_no_matching_properties",
                "chunks_scanned": scanned,
                "chunks_total": len(locations),
            }
        )
        bundle["gaps"].append(
            "Bundled-report extraction is best-effort and bounded; the property inventory may be incomplete."
        )
        if not bundle["properties"]:
            bundle["gaps"].append(
                "No properties matched the exact request, repository and tested commit; no verdict was inferred."
            )
    except (EvidenceError, OSError, ValueError, KeyError, TypeError) as exc:
        bundle["gaps"].append(
            str(exc)
            if isinstance(exc, EvidenceError)
            else f"Report input could not be read ({type(exc).__name__})"
        )
    if args.compose and args.compose.exists():
        try:
            services = load(args.compose)["services"]
            bundle["images"] = {
                name: services[name]["image"]
                for name in (
                    "p1",
                    "gov-cli",
                    "gov-configurator",
                    "tx-firehose",
                    "tracer-sidecar",
                )
            }
            env = services["gov-cli"].get("environment", {})
            bundle["test"]["options"] = {
                key: env.get(key, "false")
                for key in (
                    "LEIOS_STORAGE_CHECKS",
                    "LEIOS_BLS_CHECKS",
                    "LEIOS_PLUTUS_CHECKS",
                )
            }
            bundle["test"]["command_era"] = env.get("GOV_COMMAND_ERA", "unknown")
            bundle["test"]["protocol_version"] = (
                services["gov-configurator"]
                .get("environment", {})
                .get("PROTOCOL_VERSION", "unknown")
            )
        except (OSError, ValueError, KeyError, TypeError):
            bundle["gaps"].append("Resolved Compose provenance could not be read")
    else:
        bundle["gaps"].append("Resolved Compose provenance is unavailable")
    return bundle


def zip_member(data: bytes, name: str):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        matches = [x for x in archive.infolist() if x.filename == name]
        if len(matches) != 1 or matches[0].file_size > MAX_FILE:
            raise EvidenceError("Missing, duplicated, or oversized evidence artifact member")
        return json.loads(archive.read(matches[0]))


def github_logs(data: bytes):
    excerpts = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist()) > 1000:
            raise EvidenceError("GitHub log archive has too many entries")
        for entry in archive.infolist():
            total += entry.file_size
            if total > MAX_TOTAL or entry.file_size > MAX_FILE:
                raise EvidenceError("GitHub log archive exceeded the uncompressed size limit")
            if entry.is_dir():
                continue
            lines = archive.read(entry).decode(errors="replace").splitlines()
            for number, line in enumerate(lines, 1):
                if not re.search(
                    r"##\[error\]|outcome: (success|failure|unknown)|Process completed with exit|Error:|timed out|giving up|still pending",
                    line,
                    re.I,
                ):
                    continue
                # Do not retain wallet, credential, or arbitrary Moog JSON dumps.
                if SENSITIVE.search(line) or "{" in line or len(line) > 3000:
                    continue
                excerpts.append(
                    {"file": clean(entry.filename), "line": number, "text": clean(line)[:1500]}
                )
    return {
        "status": "collected",
        "selection": "bounded diagnostic excerpts only; full logs are not included",
        "excerpts": excerpts[-200:],
    }


def verify_run(run, repository, attempt=None):
    if (
        not isinstance(run, dict)
        or not isinstance(run.get("repository"), dict)
        or not isinstance(run.get("head_repository"), dict)
    ):
        raise EvidenceError("Source workflow repository identity is unavailable")
    if (
        run.get("repository", {}).get("full_name", "").lower() != repository.lower()
        or run.get("head_repository", {}).get("full_name", "").lower() != repository.lower()
        or run.get("path") != WORKFLOW
        or run.get("head_branch") != "main"
        or run.get("event") not in ("schedule", "workflow_dispatch")
        or run.get("status") != "completed"
    ):
        raise EvidenceError("Source must be a completed, same-repository Antithesis run on main")
    if attempt and run.get("run_attempt") != attempt:
        raise EvidenceError("Source workflow attempt does not match the requested attempt")


def finalize(args):
    downloader = Downloader()
    token = os.environ.get("GH_TOKEN", "")
    repository = args.repository or os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise EvidenceError("A repository identity is required")

    def api(path, binary=False):
        data = downloader.get(
            "https://api.github.com/repos/" + repository + "/" + path,
            token=token,
            github_redirect=binary,
        )
        return data if binary else json.loads(data)

    if args.github_run_json:
        run = load(args.github_run_json)
    else:
        if not args.github_run_id or not args.github_run_id.isdigit():
            raise EvidenceError("A numeric source GitHub run ID is required")
        endpoint = f"actions/runs/{args.github_run_id}"
        if args.expected_attempt:
            endpoint += f"/attempts/{args.expected_attempt}"
        run = api(endpoint)
    verify_run(run, repository, args.expected_attempt)
    run_id, attempt = str(run["id"]), run["run_attempt"]
    seed = None
    gaps = []
    try:
        if args.seed:
            seed = load(args.seed)
        elif not args.github_run_json:
            name = f"antithesis-report-evidence-{run_id}-{attempt}"
            candidates = []
            for page in range(1, 11):
                artifacts = api(f"actions/runs/{run_id}/artifacts?per_page=100&page={page}")[
                    "artifacts"
                ]
                candidates += [a for a in artifacts if a["name"] == name and not a["expired"]]
                if len(artifacts) < 100:
                    break
            if len(candidates) != 1:
                raise EvidenceError("Source report artifact is missing, expired or ambiguous")
            seed = zip_member(
                api(f"actions/artifacts/{int(candidates[0]['id'])}/zip", binary=True),
                "evidence.json",
            )
        if seed is not None:
            if (
                seed.get("schema_version") != 1
                or str(seed["github"]["run_id"]) != run_id
                or seed["github"]["run_attempt"] != attempt
                or seed["github"]["repository"].lower() != repository.lower()
                or seed["github"]["head_sha"] != run["head_sha"]
            ):
                raise EvidenceError("Source report artifact provenance does not match GitHub")
    except (EvidenceError, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        seed = None
        gaps.append(
            str(exc)
            if isinstance(exc, EvidenceError)
            else "Source report artifact could not be read"
        )
    bundle = (
        seed
        if seed is not None
        else {
            "schema_version": 1,
            "test": {"outcome": "unknown", "mode": "unknown"},
            "report": {"status": "unavailable", "inventory_complete": False},
            "properties": [],
            "gaps": [],
        }
    )
    if seed is None:
        gaps.append(
            "Antithesis evidence unavailable; GitHub workflow failure is not a node failure diagnosis"
        )
    bundle["github"] = {
        "repository": repository,
        "run_id": run_id,
        "run_attempt": attempt,
        "head_sha": run["head_sha"],
        "conclusion": run["conclusion"],
        "url": run["html_url"],
    }
    bundle["gaps"].extend(gaps)
    try:
        data = (
            read(args.github_logs_zip)
            if args.github_logs_zip
            else api(f"actions/runs/{run_id}/attempts/{attempt}/logs", binary=True)
        )
        bundle["github_logs"] = github_logs(data)
    except (EvidenceError, OSError, ValueError, zipfile.BadZipFile) as exc:
        bundle["github_logs"] = {"status": "unavailable"}
        bundle["gaps"].append(
            str(exc) if isinstance(exc, EvidenceError) else "GitHub logs could not be read"
        )
    return bundle


def write_bundle(bundle, output):
    bundle = clean(bundle)
    output.mkdir(parents=True, exist_ok=True)
    (output / "evidence.json").write_text(json.dumps(bundle, indent=2) + "\n")
    nonpassing = [p for p in bundle.get("properties", []) if p.get("passed") is False]
    lines = [
        "# Antithesis evidence (no AI diagnosis)",
        "",
        f"- GitHub run: {bundle['github'].get('run_id', 'unknown')}, attempt {bundle['github'].get('run_attempt', 'unknown')}",
        f"- Mode: {bundle.get('test', {}).get('mode', 'unknown')}",
        f"- Antithesis outcome: {bundle.get('test', {}).get('outcome', 'unknown')}",
        f"- Tested commit: {bundle.get('test', {}).get('tested_commit', 'unknown')}",
        f"- Report extraction: {bundle['report']['status']}; inventory completeness is not guaranteed.",
        f"- Recovered property records: {len(bundle.get('properties', []))}; nonpassing records: {len(nonpassing)}.",
        "",
        "Nonpassing records can include platform diagnostics. This is not a root-cause classification.",
        "",
    ]
    for prop in nonpassing:
        label = html.escape(str(prop.get("group_id") or prop["property_id"])).replace("\n", " ")
        lines.append(f"- {label}")
    lines += ["", "Evidence gaps:"] + ["- " + html.escape(str(gap)) for gap in bundle["gaps"]]
    (output / "summary.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report")
    for name in ("context", "facts", "compose", "report-url-file", "offline-report"):
        report.add_argument("--" + name, type=pathlib.Path)
    report.add_argument("--max-chunks", type=int, choices=range(1, 9), default=3)
    final = commands.add_parser("finalize")
    for name in ("seed", "github-run-json", "github-logs-zip"):
        final.add_argument("--" + name, type=pathlib.Path)
    final.add_argument("--repository")
    final.add_argument("--github-run-id")
    final.add_argument("--expected-attempt", type=int)
    for sub in (report, final):
        sub.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        bundle = collect_report(args) if args.command == "report" else finalize(args)
        write_bundle(bundle, args.output)
    except (EvidenceError, OSError, ValueError, KeyError, TypeError) as exc:
        print(
            str(exc)
            if isinstance(exc, EvidenceError)
            else f"Evidence collection unavailable ({type(exc).__name__})"
        )
        return 1
    print(
        "Sanitized evidence.json and summary.md written; no diagnosis or repository changes performed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
