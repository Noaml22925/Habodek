"""Run the whole corpus through the agent and collect its findings for scoring.

    python local/run_corpus.py
    python -m habodek.cli eval --from-json .habodek-work/results.json --verbose

Drives `cmd/cli review` once per case rather than labelling the MRs and waiting
on webhooks. Three reasons that is the better path for a scoring run:

* it is a **dry run** -- nothing is posted, so the corpus stays uniform and the
  next run starts from the same baseline as this one;
* it is **sequential**, which an 8B model on one GPU is anyway;
* the job id is printed, so each case's findings can be read back without
  guessing which job file belongs to which MR.

The trade is an interactive prompt at the end of a review that found something.
We answer `n`.

Findings come from the review job the CLI writes under
`agent/resource/data/review_jobs/<uuid>.json`. `aiOutputParsed` there is Go's
`[]byte`, which `encoding/json` writes as a base64 string, not as an array.

Needs the stack up: GitLab on :8929, Ollama on :11434. Budget 40-90s per case.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
from seed_gitlab import read_env_file

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"
CORPUS = REPO_ROOT / "eval" / "corpus" / "synthetic"
WORK = REPO_ROOT / ".habodek-work"

DEFAULT_RESULTS = WORK / "results.json"
DEFAULT_AGENT_ENV = AGENT_DIR / ".env"

# Every seeded case is a project of its own with exactly one MR.
MR_IID = 1

JOB_ID_RE = re.compile(r"Review job:\s*([0-9a-fA-F-]{36})")


class RunError(RuntimeError):
    pass


def project_ids(base_url: str, token: str) -> dict[str, int]:
    """Map project path -> id.

    Looked up rather than hardcoded: a project that is destroyed and re-seeded
    comes back with a new id, which has already happened once.
    """
    response = requests.get(
        f"{base_url.rstrip('/')}/api/v4/projects",
        headers={"PRIVATE-TOKEN": token},
        params={"per_page": 100, "simple": "true", "membership": "true"},
        timeout=30,
    )
    response.raise_for_status()
    return {project["path"]: project["id"] for project in response.json()}


def build_cli(binary: Path) -> None:
    binary.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/cli"],
        cwd=AGENT_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RunError(f"go build ./cmd/cli failed:\n{result.stderr.strip()}")


def run_case(binary: Path, project_id: int, timeout: int) -> tuple[str, str]:
    """Review one MR. Returns (job id, the CLI's combined output)."""
    result = subprocess.run(
        [str(binary), "review", "--project-id", str(project_id), "--mr-id", str(MR_IID)],
        cwd=AGENT_DIR,
        input="n\n",
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = result.stdout + result.stderr

    match = JOB_ID_RE.search(output)
    if not match:
        raise RunError(f"no job id in the CLI's output (exit {result.returncode})")
    return match.group(1), output


def read_findings(data_dir: Path, job_id: str) -> tuple[str, list[dict]]:
    """Read a finished job's status and its parsed comments."""
    path = data_dir / "review_jobs" / f"{job_id}.json"
    if not path.is_file():
        raise RunError(f"job file missing: {path}")

    job = json.loads(path.read_text(encoding="utf-8"))
    status = job.get("status", "unknown")

    parsed = job.get("aiOutputParsed")
    if not parsed:
        return status, []
    if isinstance(parsed, str):
        try:
            parsed = base64.b64decode(parsed)
        except (binascii.Error, ValueError) as exc:
            raise RunError(f"aiOutputParsed is not base64: {exc}") from exc

    # A review that found nothing marshals its nil slice as `null`, not `[]`.
    # That is the correct answer on the clean cases, so it must not read as an error.
    comments = json.loads(parsed)
    if comments is None:
        return status, []
    if not isinstance(comments, list):
        raise RunError(f"aiOutputParsed is {type(comments).__name__}, expected a list")
    return status, comments


def main() -> int:
    env = read_env_file(DEFAULT_AGENT_ENV)

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--token",
        default=env.get("GITLAB_TOKEN"),
        help="GitLab token with api scope (default: GITLAB_TOKEN from agent/.env)",
    )
    parser.add_argument(
        "--base-url",
        default=env.get("GITLAB_BASE_URL", "http://localhost:8929"),
        help="GitLab base URL (default: GITLAB_BASE_URL from agent/.env)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_RESULTS,
        help=f"Results file (default: {DEFAULT_RESULTS})",
    )
    parser.add_argument("--only", action="append", help="Run only this case; repeatable")
    parser.add_argument(
        "--timeout", type=int, default=600, help="Seconds to allow one review (default: 600)"
    )
    parser.add_argument(
        "--keep-logs",
        action="store_true",
        help="Write each case's CLI output next to the results file, for debugging a bad run",
    )
    args = parser.parse_args()

    if not args.token or args.token.startswith("FILL-ME"):
        parser.error("no usable token: pass --token or set GITLAB_TOKEN in agent/.env")

    if not CORPUS.is_dir():
        parser.error(f"no corpus at {CORPUS}, run `python eval/build_corpus.py` first")

    case_ids = sorted(
        d.name for d in CORPUS.iterdir() if d.is_dir() and (d / "case.yaml").is_file()
    )
    if args.only:
        wanted = set(args.only)
        unknown = wanted - set(case_ids)
        if unknown:
            parser.error(f"unknown case(s): {', '.join(sorted(unknown))}")
        case_ids = [c for c in case_ids if c in wanted]

    try:
        projects = project_ids(args.base_url, args.token)
    except requests.RequestException as exc:
        print(f"cannot reach GitLab at {args.base_url}: {exc}", file=sys.stderr)
        return 1

    unseeded = [c for c in case_ids if c not in projects]
    if unseeded:
        print(f"not seeded in GitLab: {', '.join(unseeded)}", file=sys.stderr)
        print("run `python local/seed_gitlab.py --webhook` first", file=sys.stderr)
        return 1

    binary = WORK / ("habodek-cli.exe" if sys.platform == "win32" else "habodek-cli")
    print(f"building {binary.name}...")
    try:
        build_cli(binary)
    except RunError as exc:
        print(exc, file=sys.stderr)
        return 1

    data_dir = AGENT_DIR / env.get("STORE_DATA_DIR", "resource/data")
    log_dir = args.out.with_suffix(".logs")
    if args.keep_logs:
        log_dir.mkdir(parents=True, exist_ok=True)

    print(f"{len(case_ids)} cases against {args.base_url}\n")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    results: dict[str, list[dict]] = {}
    failed: list[str] = []
    started = time.monotonic()

    for case_id in case_ids:
        project_id = projects[case_id]
        mark = time.monotonic()
        try:
            job_id, output = run_case(binary, project_id, args.timeout)
            if args.keep_logs:
                (log_dir / f"{case_id}.log").write_text(output, encoding="utf-8")
            status, comments = read_findings(data_dir, job_id)
        except subprocess.TimeoutExpired:
            print(f"  {case_id:<28} TIMED OUT after {args.timeout}s", file=sys.stderr)
            failed.append(case_id)
            continue
        except (RunError, json.JSONDecodeError) as exc:
            print(f"  {case_id:<28} FAILED: {exc}", file=sys.stderr)
            failed.append(case_id)
            continue

        results[case_id] = comments
        suppressed = sum(1 for c in comments if c.get("suppressed"))
        note = f"{len(comments) - suppressed} findings"
        if suppressed:
            note += f" (+{suppressed} suppressed)"
        if status.upper() != "COMPLETED":
            note += f"  [{status}]"
            failed.append(case_id)
        # Written after every case, not at the end: this run is 20 minutes of GPU
        # time and an interrupt half way through should cost the remaining cases,
        # not the finished ones. Same reason for flush -- a pipe otherwise buffers
        # the whole run's output to the end, so a long run looks hung.
        args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(
            f"  {case_id:<28} project {project_id:<4} {note:<32} {time.monotonic() - mark:5.0f}s",
            flush=True,
        )

    elapsed = (time.monotonic() - started) / 60
    print(f"\n{len(results)}/{len(case_ids)} cases collected in {elapsed:.1f} min -> {args.out}")
    if failed:
        print(f"needs a look: {', '.join(failed)}", file=sys.stderr)
    print(f"\nScore it:\n  python -m habodek.cli eval --from-json {args.out} --verbose")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
