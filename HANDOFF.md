# Handoff — getting Habodek v2 onto the VPN machine

This file covers moving the repository across and what the VPN host needs. Then work
from [`DEPLOY.md`](DEPLOY.md), the deployment checklist. [`ROADMAP.md`](ROADMAP.md)
is the decision record: it explains *why* anything here is the way it is.

---

## 1. Move the code as a git bundle

Work on branch `habodek-brain-rework`. On a machine with internet access:

```bash
git clone -b habodek-brain-rework https://github.com/Noaml22925/Habodek.git
cd Habodek && git bundle create ../habodek.bundle habodek-brain-rework
```

Carry `habodek.bundle` across, then on the VPN machine:

```bash
git clone -b habodek-brain-rework habodek.bundle Habodek
git -C Habodek log --oneline -1      # the commit you expect
```

A bundle keeps the history; a zip of the working tree does not, and loses the
`.gitignore`d files anyway. Nothing machine-specific travels: the configuration files
(`docker/habodek.env`, `docker/compose.env`) are created on the VPN host from their
`.example` templates, and **must never be copied from a development machine** — they
hold the GitLab token and the gateway key.

## 2. What the VPN host needs

| Need | Why |
|---|---|
| Linux x86_64, Docker 24+ with the compose plugin | everything runs in containers; the control plane starts sandbox containers through the Docker socket |
| git | only to clone the bundle |
| The Artifactory **Docker** proxy URL (e.g. `artifactory.example/docker-remote`) | base images: `python:3.12-bookworm`, `python:3.12-slim`, `golang:1.25.5-bookworm`, `docker:27.5-cli`, `postgres:16` |
| The Artifactory **PyPI** proxy (`…/api/pypi/pypi/simple`) | Python packages for the images, and for repositories' dependencies in the sandbox |
| CPU and memory | roughly 4 cores and 8 GB for the service, plus 2 cores / 4 GB per concurrent sandbox run |
| Disk under one data directory (default `/var/lib/habodek`) | mirrors, worktrees, traces (kept), memory, venv cache, Postgres volume |

No Go and no Python are needed on the host: the images build them in. Nothing calls
out at runtime except the gateway and GitLab — `docker/build.sh` proves it (the
egress test) before you deploy.

## 3. How the build stays offline

- **Go:** the dependencies are vendored in `agent/vendor/`; the image builds with
  `GOFLAGS=-mod=vendor GOPROXY=off GOTOOLCHAIN=local`, so it needs no module proxy and
  no checksum database. The Go base image must be ≥ 1.25.5 (what `agent/go.mod`
  declares). Refreshing the vendor tree (`cd agent && go mod vendor`) happens off-VPN.
- **Python:** every package comes through the Artifactory PyPI proxy, including
  `ripgrep` (its PyPI wheel carries the `rg` binary), so the images need no apt.
- **Base images:** through the Artifactory Docker proxy (owner, 2026-11-24).

## 4. If you edit on Windows

`.gitattributes` pins `eol=lf`, but a global `core.autocrlf=true` overrides it and the
tree drifts to CRLF, which breaks the shell scripts in `docker/`:

```bash
git config core.autocrlf false
git status --porcelain               # must be empty before the next line
git ls-files -z | xargs -0 rm -f && git checkout -- .
```

## 5. Development checks (optional on the VPN host)

On a machine with Go ≥ 1.25.5 and Python 3.12:

```bash
pip install -e ".[dev]" && pytest -q && ruff check .
cd agent && go build ./... && go vet ./... && go test -race ./...
HABODEK_E2E=1 go test -run TestEndToEnd ./internal/orchestrator/
docker/egress/run.sh --netns          # the egress test without Docker (needs unshare)
```

## 6. Then: `DEPLOY.md`

Build the images, configure, start, register the webhook, review a first MR, and run
the acceptance test.
