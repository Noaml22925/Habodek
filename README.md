# Habodek

An automatic code-review agent for our GitLab merge requests, running on the
company LiteLLM gateway.

It participates in the normal MR flow rather than replacing it: it reads the change
*and the surrounding code*, opens threads on exact diff lines, answers when a
reviewer replies, and resolves its own threads once they are addressed.

## Layout

```
agent/    The reviewer itself. Go. A fork of antlss/gitlab-review-agent (MIT) —
          see agent/NOTICE.md for provenance and the list of our changes.
eval/     Evaluation harness. Python. A corpus of code changes with deliberately
          planted flaws, and a scorer that grades any reviewer's output.
habodek/  Python support code for the harness (diff parsing, finding schema).
```

The two halves are deliberately independent. `eval/` grades *any* reviewer — it was
built to compare candidates before we picked one, and it stays useful afterwards as
the regression check on prompt and model changes.

## Why a fork

We evaluated building this from scratch, adopting [PR-Agent][pr-agent], and forking
[gitlab-review-agent][upstream].

gitlab-review-agent already does the two things that mattered most and that
PR-Agent does not: **agentic exploration** of the repository (`read_file`,
`search_code`, `read_multi_file` — it can go and look at the caller before
commenting) and a **replier agent** that reads thread history plus surrounding code
and continues the discussion. It is MIT, it talks to any OpenAI-compatible endpoint,
and it is label-triggered rather than firing on every push.

Its problem is that it is dormant — 15 commits, one author, nothing since April
2026. So we own it now. Full reasoning is in [`ROADMAP.md`](ROADMAP.md).

## Running it

The agent needs no local Go toolchain to run — the Dockerfile builds it:

```bash
cd agent
cp .env.example .env      # fill in GitLab and LiteLLM details
docker compose up --build
```

Point a GitLab project webhook (Merge request + Comment events) at
`http://<host>:8080/webhook`, using the same secret as `GITLAB_WEBHOOK_SECRET`.
Add the `ai-review` label to an MR to trigger a review.

For development, install Go 1.25+ (`winget install GoLang.Go`) and:

```bash
cd agent
go build ./...
go test ./...
go test -race ./...       # always, before touching anything concurrent
go run ./cmd/cli review   # interactive dry run: pick which comments to post
```

See [`agent/README.md`](agent/README.md) for configuration against our gateway, and
[`agent/README.upstream.md`](agent/README.upstream.md) for the original author's
full feature documentation.

## Evaluating a change

Before and after any prompt or model change:

```bash
pip install -e ".[dev]"
python -m habodek.cli eval --list              # the corpus
python -m habodek.cli eval --materialise ./cases
python -m habodek.cli eval --from-json out.json --verbose
```

Read **noise on clean cases first**, detection last. A reviewer that flags
everything scores perfectly on detection and is worthless in practice — two corpus
cases are clean changes where the correct review is silence.

[upstream]: https://github.com/antlss/gitlab-review-agent
[pr-agent]: https://github.com/qodo-ai/pr-agent
