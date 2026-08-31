# The local stack

Everything the reviewer needs, on this machine, with no VPN: a real GitLab, a
real model, real webhooks. What it cannot tell you is whether the *reviews are
any good* — an 8B model validates plumbing only.

## Prerequisites

| | Status on this machine (2026-08-31) |
|---|---|
| Go 1.27 | installed, `C:\Program Files\Go\bin` |
| Ollama + `qwen3:8b` | installed |
| WSL 2 | **not installed** — needs an elevated `wsl --install` and a reboot |
| Docker Desktop | **not installed** — needs WSL 2 first |

Windows 11 Home has no Hyper-V, so Docker Desktop's WSL 2 backend is the only
option and WSL is a hard prerequisite. From an **administrator** terminal:

```powershell
wsl --install --no-distribution   # then reboot
winget install --id Docker.DockerDesktop
```

Then, before the first `docker compose up`: **Docker Desktop → Settings →
Resources → Advanced → Disk image location → move it to `D:`.** GitLab wants
around 10GB and `C:` is down to roughly 20GB free.

## GitLab

```bash
cd local
cp .env.example .env        # set a real password, GitLab rejects weak ones
docker compose up -d
docker compose logs -f gitlab
```

First boot takes 5-10 minutes. **Do not trust the container's health status** —
`gitlab-healthcheck` reports healthy several minutes before GitLab serves a
request, and it stays healthy through a failed reconfigure. The honest readiness
check is an HTTP one:

```bash
curl -o /dev/null -w '%{http_code}\n' http://localhost:8929/users/sign_in   # 200 = ready
```

A reconfigure that dies leaves `FATAL:` in `docker compose logs gitlab`. The
usual cause is an unknown key in `GITLAB_OMNIBUS_CONFIG` — the omnibus aborts the
entire run over one retired option rather than skipping it.

## The bot user, its token, and the webhook setting

None of this needs the UI:

```bash
docker exec -i habodek-gitlab gitlab-rails runner - < local/bootstrap_gitlab.rb
```

That creates the `habodek-bot` user, issues it an `api`-scoped token, and turns
on outbound local requests. It prints `GITLAB_BOT_USER_ID` and `GITLAB_TOKEN` —
copy both into `agent/.env`, replacing the `FILL-ME` values. Allow it 30-60s;
most of that is Rails booting. Re-running it is safe and reissues the token.

Two things it sets that are easy to miss by hand:

- **`GITLAB_BOT_USER_ID` is the self-reply guard.** Point it at the wrong user
  and the agent answers its own comments in a loop.
- **Outbound local requests must be allowed** or GitLab refuses to call the agent
  on the host, and the webhook silently never fires.

Webhooks are per-project, so the seeder registers them — `--webhook`, below. By
hand it would be Project → Settings → Webhooks → URL
`http://host.docker.internal:8080/webhook/gitlab`, secret token matching
`GITLAB_WEBHOOK_SECRET`, triggers *Merge request events* and *Comments*.
`localhost` from inside the container is the container, not the host.

The root password is in `local/.env` if you want to sign in and look around.

## The corpus as merge requests

The 13 cases are materialised as two-commit git repos:

```bash
python -m habodek.cli eval --materialise .habodek-work/cases
python local/seed_gitlab.py --webhook          # token read from agent/.env
```

The seeder creates one project per case, pushes the base commit as `main` and the
change commit as `change`, opens the MR, and with `--webhook` points the project
at the agent — so the diff GitLab shows is exactly the diff the corpus expects.
Re-running it converges rather than duplicating.

It labels nothing by default, because the `ai-review` label is what triggers a
review. Seed all 13, then trigger one and watch it before turning the rest loose:

```bash
python local/seed_gitlab.py --label off-by-one-pagination
python local/seed_gitlab.py --label all      # when you trust it
```

Three cases (`swallowed-exception`, `lsp-violation`, `dip-hardcoded-dependency`)
hide the defect *outside* the changed lines. They are the ones that separate a
reviewer that explores the repository from one that only reads the diff, so read
those results first.

## The model

The server must be running before the agent starts:

```bash
ollama serve          # or just launch the Ollama tray app
ollama list           # qwen3:8b should be there
```

Two settings have to move together: `OLLAMA_CONTEXT_LENGTH` (what the server
will actually hold) and `LLM_CONTEXT_WINDOWS` in `agent/.env` (what the agent
believes it has). Both are 16384. If the agent's number is the larger one Ollama
truncates the prompt without an error and the review quietly degrades; 16k is
also about what fits in 8GB of VRAM next to the model itself.

## Running the agent

```bash
cd agent
go run ./cmd/server            # webhook listener on :8080
go run ./cmd/cli review --project-id <id> --mr-id <iid>   # interactive dry run
```

The dry run is the cheaper first move — it prints findings and lets you pick
which to post, so a bad first review costs nothing. It still needs a live GitLab:
it fetches the project and MR over the API before it does anything.
