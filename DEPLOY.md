# Deploying Habodek v2

The checklist for the VPN host, top to bottom; each step can be checked before the
next. It assumes the repository is on the host ([`HANDOFF.md`](HANDOFF.md)). Every
command that runs a check prints at most 20 lines and says where its details are.

Configuration reference: [`agent/README.md`](agent/README.md). Why things are as
they are: [`ROADMAP.md`](ROADMAP.md).

---

## 0. Before you start, have at hand

- the Artifactory Docker proxy prefix (`REGISTRY`) and PyPI simple URL;
- the LiteLLM gateway URL and key, and the two models' aliases on it;
- a GitLab **bot account** (step 3) and the projects it should review;
- the host's Docker group id: `getent group docker | cut -d: -f3`.

## 1. Probe the gateway (once, ~20 minutes)

Standard library only; nothing to install. It settles the model names, limits and
reasoning behaviour the brain's defaults were guessed from:

```bash
export LITELLM_KEY=...
python3 probe/gateway_probe.py --base-url https://<litellm-host>/v1 --key-env LITELLM_KEY \
    --model qwen=<qwen alias> --model glm=<glm alias>
```

Send the printed lines back to whoever maintains Habodek; `habodek/brain/llm/models.yaml`
changes in one place if the gateway differs from the defaults.

## 2. Build the images and run the egress test (~15-30 minutes)

```bash
docker/build.sh --registry <REGISTRY> --pip-index <PyPI simple URL> --tag v2-1
```

Expected, in about 10 lines: `ok brain`, `ok control`, `ok runner`, `ok runner-check`,
`ok server-check`, the licence summary, then `egress PASS … scorecard ok: 9/9 …
e2e ok … outbound attempts: none`. It builds `habodek-brain`, `habodek-control` and
`habodek-runner`, writes `build-logs/<time>/` (each step's log, `licences.tsv`,
`images.txt`, `egress.log`).

| If it says | Do |
|---|---|
| `FAIL brain` / `control` / `runner` pulling a base image | the tag is not in the proxy: check `docker pull <REGISTRY>/<image>`; tags are in the build args of `docker/build.sh` |
| `FAIL control` with a Go version error | the Go image is older than 1.25.5; use a newer `golang:1.25.x-bookworm` |
| `licences: … N to review` | read the flagged rows in `licences.tsv` (copyleft or unidentified); nothing ships GPL today |
| `egress FAIL … outbound attempts` | **stop**: something tried to reach the network. `egress.log` names the process and target |

The egress test runs the full scenario suite and the end-to-end test inside the
control image with `--network none`, with an audit hook and a proxy sink that would
record any attempt to leave.

## 3. The bot account

Create a GitLab user for Habodek (e.g. `habodek-bot`), add it to each project as
**Developer**, and create a personal access token with the `api` scope. Note its
numeric user id (Admin → Users, or `GET /api/v4/user` with the token): the bot
recognises its own notes by it and never answers itself.

## 4. Prepare the host

```bash
sudo mkdir -p /var/lib/habodek && sudo chown 10001 /var/lib/habodek   # the image's user
cd docker
cp habodek.env.example habodek.env && chmod 600 habodek.env   # fill in every FILL-ME
cp compose.env.example compose.env && chmod 600 compose.env   # REGISTRY, tag, Postgres password
```

In `habodek.env`: GitLab URL, token, webhook secret (any long random string), bot user
id; gateway URL, key and model aliases; `HABODEK_INTERNAL_PACKAGES` (the internal
Python package names or globs whose source the reviewer should read, e.g.
`platform-*,payments-lib`) and `HABODEK_PIP_INDEX_URL`. In `compose.env`, set
`HABODEK_TAG` to the tag you built.

If the data directory is not `/var/lib/habodek`, set `HABODEK_DATA` in `compose.env`;
compose then maps paths for the sandbox (`HABODEK_DOCKER_PATH_MAP`).

## 5. Start it

```bash
DOCKER_GID=$(getent group docker | cut -d: -f3) docker compose --env-file compose.env up -d
curl -s localhost:8080/health                       # {"status":"ok"}
docker compose --env-file compose.env logs habodek | tail -5
```

The log line `habodek control plane listening` shows the store (`postgres`), the
worker count, the gateway limit and `sandbox docker`.

## 6. Register the webhook

For each project: Settings → Webhooks → URL `http://<host>:8080/webhook/gitlab`,
secret = `GITLAB_WEBHOOK_SECRET`, events **Merge request**, **Comments** and **Push**
(push events let the cartographer re-map a project after structural merges). Send a
test event; the log shows it.

## 7. Map the libraries' consumers first

A library MR is reviewed against the projects that use the library, but a consumer is
known only once it has been mapped. For each project that depends on an internal
library, map it once (a background run, up to 300 requests):

```bash
docker compose --env-file compose.env exec habodek habodek map --project <id>
```

Every project is also mapped automatically after its first review.

## 8. A first real review

Add the label `ai-review` to an MR. Within seconds the bot posts "Habodek is
reviewing this merge request"; findings arrive as threads when the review is done
(typically 15-30 minutes: requests, not tokens, are the limit — about 25 per minute
per model). A clean MR gets one summary note and no threads.

To see why it said what it said, or why it was silent:

```bash
docker compose --env-file compose.env exec habodek ls /var/lib/habodek/traces
docker compose --env-file compose.env exec habodek \
    habodek-brain trace show /var/lib/habodek/traces/<job id>
# open /var/lib/habodek/traces/<job id>/trace.html on the host in a browser
```

Reply in a thread ("fixed", "this is handled in X", "by design") and the thread agent
answers within a few minutes, ahead of any queued review.

## 9. The acceptance test

The criteria are in `docs/reviewer-v2-handoff.md` §8. In order:

1. **Scenarios** — the brain image runs them against the real gateway (the sandbox is
   off inside a container, which the header line says):

   ```bash
   docker run --rm -v /var/lib/habodek:/var/lib/habodek \
     -e HABODEK_LLM_BASE_URL=https://<litellm-host>/v1 -e HABODEK_LLM_TOKEN=<key> \
     -e HABODEK_MODEL_QWEN=<qwen alias> -e HABODEK_MODEL_GLM=<glm alias> \
     habodek-brain:<tag> scorecard --scenarios 1-9 --runs 3
   ```

   S1-S2 found with correct chains in at least 2 of 3 runs; S3-S4 silent in 3 of 3;
   S5-S9 correct verdicts (S7 and S9 in 3 of 3, the rest 2 of 3). Two to four hours.
2. **One real MR end to end** (step 8), its trace viewable.
3. **No outbound connection except the gateway and GitLab** — the egress test in
   step 2.
4. **Replies ahead of reviews** — label two MRs, then reply in an existing thread:
   the reply is answered before the second review starts.
5. **Comparison** — read Habodek's comments next to the Claude Code references in
   `eval/scenarios/s*/reference_claude_code.md`.

## 10. Operating it

- **Acceptance and bars:** `docker compose … exec habodek habodek metrics --project <id>`
  or `GET /internal/metrics?project=<id>`. A category developers keep rejecting
  automatically posts only its more severe findings; the log says `bar raised`.
- **Memory:** `/var/lib/habodek/memory` is a git repository of markdown (repo cards,
  lessons). Back it up like code. People may edit a card's *Team notes* section and a
  lesson's `status:` line (`active` / `retired`); commit the edit.
- **Team rules:** a `.habodek.md` in a repository (any directory) adds checks for the
  code under it, read from the target branch. It cannot suppress findings.
- **Traces** are kept and not cleaned up automatically; prune
  `/var/lib/habodek/traces` by age when disk matters (each review is a few MB).
- **Replay a run** after changing a model or prompt:
  `habodek-brain replay <trace dir> --from-stage verify` (inside the container).
- **Upgrade:** rebuild with a new tag (step 2), set `HABODEK_TAG`, `docker compose up -d`.
  Queued tasks survive restarts (Postgres); a task that was running is retried.

| Symptom | Likely cause |
|---|---|
| no "is reviewing" note after labelling | webhook not delivered (Settings → Webhooks → Recent events) or wrong secret |
| `GITLAB_BOT_USER_ID is required` at start | fill it in (step 3) |
| reviews take hours | the queue: about 4-6 typical reviews per hour per model pair; more keys or a higher limit is the fix |
| `env-prep failed` in a trace | the repository's dependencies are not installable from Artifactory; the review continues without test runs |
| `sandbox … permission denied` | `DOCKER_GID` does not match the host's docker group |
