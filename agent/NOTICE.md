# Attribution

This directory is a fork of **gitlab-review-agent** by Trần Long Ẩn (Tran Long An).

| | |
|---|---|
| Upstream | https://github.com/antlss/gitlab-review-agent |
| Licence | MIT — see [`LICENSE`](LICENSE), retained unmodified |
| Forked at | `ab50d4f03c56357824b9557a2e489ac1b112f395` (2026-04-02) |
| Forked on | 2026-08-31 |
| Upstream state | 15 commits, single author, no activity since 2026-04-02 |

The MIT licence and its copyright notice are retained in [`LICENSE`](LICENSE) and
must stay there. Upstream's own README is preserved as
[`README.upstream.md`](README.upstream.md) — it documents features and options this
fork has not yet re-documented, so it is worth keeping rather than deleting.

## Why this was forked rather than depended on

Upstream is dormant and has no releases, so there is nothing to depend on and
nobody to send fixes to. Ownership was the only realistic option.

There is an upside to forking a dormant project: **there is no upstream to merge
with**, so this fork carries none of the usual divergence tax. It is simply our
code now.

To compare against upstream at any point:

```bash
git fetch upstream
git diff upstream/main -- agent/internal/pkg/llm/openai.go
```

## Changes from upstream

Recorded here as they are made, so the delta stays legible.

- **2026-08-31** — Go module path renamed from
  `github.com/antlss/gitlab-review-agent` to `habodek` (58 files). A bare module
  name is valid for a private application; change it if this is ever published or
  vendored by path. Verified: `go build ./...` and `go vet ./...` clean afterwards.
- **2026-08-31** — Upstream `README.md` moved to `README.upstream.md`; our own
  `README.md` and this file added. No behavioural change.
