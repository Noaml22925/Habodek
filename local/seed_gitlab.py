"""Push the materialised corpus cases into a local GitLab as merge requests.

    python local/seed_gitlab.py --token glpat-xxxx

Each case repo is two commits on `main`: a base, then the change under review.
This pushes the base as `main` and the change as a branch off it, so the MR diff
GitLab shows is exactly the diff the corpus expects.

Nothing is labelled by default. The agent only reacts to the `ai-review` label,
so seeding stays separate from triggering: seed all 13, then label one and watch
it before turning the rest loose.

    python local/seed_gitlab.py --token glpat-xxxx --label off-by-one-pagination

Standalone on purpose — argparse and requests, no import from `habodek`. It is an
operations script for a throwaway instance, not part of the shipped CLI.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = REPO_ROOT / ".habodek-work" / "cases"
DEFAULT_AGENT_ENV = REPO_ROOT / "agent" / ".env"

TARGET_BRANCH = "main"
SOURCE_BRANCH = "change"
TRIGGER_LABEL = "ai-review"

# From inside the container, `localhost` is the container. The host is this.
DEFAULT_WEBHOOK_URL = "http://host.docker.internal:8080/webhook/gitlab"


class SeedError(RuntimeError):
    pass


def read_env_file(path: Path) -> dict[str, str]:
    """Parse the KEY=value lines of a .env file. Absent file is not an error."""
    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SeedError(f"git {' '.join(args)} failed in {repo.name}:\n{result.stderr.strip()}")
    return result.stdout.strip()


class GitLab:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session = requests.Session()
        self.session.headers["PRIVATE-TOKEN"] = token

    def _call(self, method: str, path: str, **kwargs) -> requests.Response:
        response = self.session.request(
            method, f"{self.base_url}/api/v4{path}", timeout=30, **kwargs
        )
        if response.status_code >= 400:
            raise SeedError(f"{method} {path} → {response.status_code}: {response.text[:300]}")
        return response

    def whoami(self) -> dict:
        return self._call("GET", "/user").json()

    def find_project(self, path: str) -> dict | None:
        # The id in the API is namespace/path, and the slash has to survive as %2F.
        response = self.session.get(
            f"{self.base_url}/api/v4/projects/{quote(path, safe='')}", timeout=30
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise SeedError(f"look up {path} → {response.status_code}: {response.text[:300]}")
        return response.json()

    def create_project(self, name: str) -> dict:
        return self._call(
            "POST",
            "/projects",
            json={
                "name": name,
                "path": name,
                "visibility": "private",
                "initialize_with_readme": False,
                # The reviewer reads the repository, it does not build it.
                "builds_access_level": "disabled",
                "wiki_access_level": "disabled",
            },
        ).json()

    def find_mr(self, project_id: int) -> dict | None:
        mrs = self._call(
            "GET",
            f"/projects/{project_id}/merge_requests",
            params={"source_branch": SOURCE_BRANCH, "state": "opened"},
        ).json()
        return mrs[0] if mrs else None

    def create_mr(self, project_id: int, title: str, description: str) -> dict:
        return self._call(
            "POST",
            f"/projects/{project_id}/merge_requests",
            json={
                "source_branch": SOURCE_BRANCH,
                "target_branch": TARGET_BRANCH,
                "title": title,
                "description": description,
            },
        ).json()

    def set_labels(self, project_id: int, mr_iid: int, labels: str) -> dict:
        return self._call(
            "PUT",
            f"/projects/{project_id}/merge_requests/{mr_iid}",
            json={"labels": labels},
        ).json()

    def ensure_hook(self, project_id: int, url: str, secret: str) -> bool:
        """Register the agent's webhook. Returns True if it created one."""
        for hook in self._call("GET", f"/projects/{project_id}/hooks").json():
            if hook["url"] == url:
                return False

        self._call(
            "POST",
            f"/projects/{project_id}/hooks",
            json={
                "url": url,
                "token": secret,
                # The two the agent acts on: label changes arrive as MR events,
                # reviewer replies as note events.
                "merge_requests_events": True,
                "note_events": True,
                "push_events": False,
                "enable_ssl_verification": False,
            },
        )
        return True


def push_url(base_url: str, token: str, project_path: str) -> str:
    """An https-with-credentials remote, so pushing needs no ssh key or prompt."""
    scheme, netloc, *_ = urlsplit(base_url)
    host = netloc.rsplit("@", 1)[-1]
    return urlunsplit((scheme, f"oauth2:{token}@{host}", f"/{project_path}.git", "", ""))


def seed_case(
    gl: GitLab,
    case_dir: Path,
    namespace: str,
    base_url: str,
    hook: tuple[str, str] | None = None,
) -> tuple[dict, dict]:
    """Create the project if missing, push both branches, open the MR."""
    name = case_dir.name
    project_path = f"{namespace}/{name}"

    project = gl.find_project(project_path)
    if project is None:
        project = gl.create_project(name)

    if hook is not None:
        gl.ensure_hook(project["id"], *hook)

    head = git(case_dir, "rev-parse", "HEAD")
    base = git(case_dir, "rev-parse", "HEAD~1")
    title = git(case_dir, "log", "-1", "--format=%s")

    remote = push_url(base_url, gl.token, project["path_with_namespace"])

    # Force-push both refs so re-running the script converges rather than failing
    # halfway. These are disposable projects on a local instance.
    git(case_dir, "push", "--force", remote, f"{base}:refs/heads/{TARGET_BRANCH}")
    git(case_dir, "push", "--force", remote, f"{head}:refs/heads/{SOURCE_BRANCH}")

    mr = gl.find_mr(project["id"])
    if mr is None:
        mr = gl.create_mr(
            project["id"],
            title,
            f"Corpus case `{name}`. Seeded by `local/seed_gitlab.py`.",
        )

    return project, mr


def main() -> int:
    env = read_env_file(DEFAULT_AGENT_ENV)

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--token",
        default=env.get("GITLAB_TOKEN"),
        help="GitLab personal access token with api scope (default: GITLAB_TOKEN from agent/.env)",
    )
    parser.add_argument(
        "--base-url",
        default=env.get("GITLAB_BASE_URL", "http://localhost:8929"),
        help="GitLab base URL (default: GITLAB_BASE_URL from agent/.env)",
    )
    parser.add_argument(
        "--cases", type=Path, default=DEFAULT_CASES, help="Directory of materialised case repos"
    )
    parser.add_argument("--only", action="append", help="Seed only this case; repeatable")
    parser.add_argument(
        "--label",
        action="append",
        metavar="CASE",
        help=f"Add the {TRIGGER_LABEL!r} label to this case's MR, triggering a review; repeatable. "
        "Pass 'all' to label every seeded case.",
    )
    parser.add_argument(
        "--webhook",
        nargs="?",
        const=DEFAULT_WEBHOOK_URL,
        metavar="URL",
        help=f"Register the agent's webhook on each project (default: {DEFAULT_WEBHOOK_URL}). "
        "The secret comes from GITLAB_WEBHOOK_SECRET in agent/.env.",
    )
    args = parser.parse_args()

    if not args.token or args.token.startswith("FILL-ME"):
        parser.error("no usable token: pass --token or set GITLAB_TOKEN in agent/.env")

    hook = None
    if args.webhook:
        secret = env.get("GITLAB_WEBHOOK_SECRET", "")
        if not secret:
            parser.error("--webhook needs GITLAB_WEBHOOK_SECRET set in agent/.env")
        hook = (args.webhook, secret)

    case_dirs = (
        sorted(d for d in args.cases.iterdir() if (d / ".git").is_dir())
        if args.cases.is_dir()
        else []
    )
    if not case_dirs:
        parser.error(
            f"no case repos under {args.cases} — "
            "run `python -m habodek.cli eval --materialise` first"
        )

    if args.only:
        wanted = set(args.only)
        case_dirs = [d for d in case_dirs if d.name in wanted]
        missing = wanted - {d.name for d in case_dirs}
        if missing:
            parser.error(f"unknown case(s): {', '.join(sorted(missing))}")

    gl = GitLab(args.base_url, args.token)
    try:
        user = gl.whoami()
    except (SeedError, requests.RequestException) as exc:
        print(f"cannot reach GitLab at {args.base_url}: {exc}", file=sys.stderr)
        return 1

    namespace = user["username"]
    print(f"{args.base_url} as {namespace} (user id {user['id']})\n")

    to_label = set(args.label or [])
    label_all = "all" in to_label
    seeded: list[str] = []
    failed: list[str] = []

    for case_dir in case_dirs:
        try:
            project, mr = seed_case(gl, case_dir, namespace, args.base_url, hook)
            note = ""
            if label_all or case_dir.name in to_label:
                gl.set_labels(project["id"], mr["iid"], TRIGGER_LABEL)
                note = f"  [{TRIGGER_LABEL}]"
            where = f"project {project['id']:<4} !{mr['iid']}"
            print(f"  {case_dir.name:<28} {where}  {mr['web_url']}{note}")
            seeded.append(case_dir.name)
        except (SeedError, requests.RequestException) as exc:
            print(f"  {case_dir.name:<28} FAILED: {exc}", file=sys.stderr)
            failed.append(case_dir.name)

    unknown = to_label - {"all"} - set(seeded)
    if unknown:
        print(f"\nnot labelled, never seeded: {', '.join(sorted(unknown))}", file=sys.stderr)

    print(f"\n{len(seeded)} seeded, {len(failed)} failed.")
    if not to_label:
        print(f"Nothing is labelled, so nothing will be reviewed. Add {TRIGGER_LABEL!r} to an MR")
        print("in the UI, or re-run with --label <case>, once the agent is running.")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
