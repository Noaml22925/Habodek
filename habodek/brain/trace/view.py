"""`trace show RUN`: one static HTML file that answers "why did it say / miss that?".

    habodek-brain trace show RUN_DIR [--out FILE]     (also: habodek trace show RUN_DIR)

Offline, no server, no JavaScript: `<details>` folds the depth. Sections:

- the run (job, revisions, models, prompt hashes, memory commit, sandbox, requests);
- the outcome (summary, posted comments or the thread verdict);
- the funnel per lens (raised → investigated → confirmed → checked → upheld → posted);
- the **hypothesis tree**: each hypothesis, its investigation's conclusion and chain,
  the mechanical checks, both verifiers' arguments and evidence, and where it ended,
  with links to the agents that worked on it;
- the stages, then every agent's transcript: reasoning, tool calls with their
  arguments and results, the terminal call;
- sandbox runs, team rules, memory, the evidence pack.
"""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from habodek.brain.contract import Finding
from habodek.brain.trace.metrics import FUNNEL, run_metrics

MAX_OUTPUT = 20_000

CSS = """
:root { --fg:#1d1f23; --muted:#5f6673; --bg:#fff; --card:#f6f7f9; --line:#dfe2e7;
        --ok:#1a7f37; --bad:#b42318; --warn:#9a6700; --accent:#2457c5; }
@media (prefers-color-scheme: dark) { :root { --fg:#e6e8eb; --muted:#9aa3af; --bg:#14161a;
        --card:#1d2026; --line:#30343c; --ok:#4ac26b; --bad:#ff7b72; --warn:#d29922;
        --accent:#79a8ff; } }
body { font: 14px/1.5 system-ui, sans-serif; color: var(--fg); background: var(--bg);
       max-width: 1100px; margin: 0 auto; padding: 16px; }
h1 { font-size: 20px; } h2 { font-size: 16px; margin-top: 28px;
     border-bottom: 1px solid var(--line); padding-bottom: 4px; }
pre { background: var(--card); padding: 8px; overflow-x: auto; white-space: pre-wrap;
      word-break: break-word; border-radius: 4px; font-size: 12.5px; }
table { border-collapse: collapse; } td, th { border: 1px solid var(--line);
      padding: 3px 8px; text-align: left; vertical-align: top; }
details { margin: 4px 0; } summary { cursor: pointer; }
.box { background: var(--card); border: 1px solid var(--line); border-radius: 6px;
       padding: 8px 12px; margin: 8px 0; }
.ok { color: var(--ok); } .bad { color: var(--bad); } .warn { color: var(--warn); }
.muted { color: var(--muted); } code { font-size: 12.5px; }
.tag { display: inline-block; border: 1px solid var(--line); border-radius: 10px;
       padding: 0 6px; font-size: 12px; margin-right: 4px; }
a { color: var(--accent); }
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def pre(text: Any, limit: int = MAX_OUTPUT) -> str:
    text = "" if text is None else str(text)
    cut = ""
    if len(text) > limit:
        cut = f"\n... ({len(text) - limit} more characters in the trace files)"
        text = text[:limit]
    return f"<pre>{esc(text + cut)}</pre>"


def anchor(label: str) -> str:
    return "agent-" + re.sub(r"[^A-Za-z0-9_-]", "-", label)


def status_class(status: str) -> str:
    return {"posted": "ok", "upheld": "ok", "confirmed": "ok", "concluded": "ok",
            "dropped": "bad", "refuted": "bad", "error": "bad", "failed": "bad",
            "not_investigated": "warn", "uncertain": "warn", "inconclusive": "warn"}.get(
        status, "muted")


# --------------------------------------------------------------------------- loading


class Run:
    def __init__(self, run_dir: Path) -> None:
        self.dir = Path(run_dir)
        self.events = [json.loads(line) for line in
                       (self.dir / "events.jsonl").read_text().splitlines() if line] \
            if (self.dir / "events.jsonl").is_file() else []
        self.info = self._json("run.json") or {}
        self.result = self._json("result.json") or {}
        raw = self._json("findings.json") or self.result.get("findings") or []
        self.findings = [Finding.model_validate(f) for f in raw]
        self.metrics = self._json("metrics.json") or run_metrics(self.findings)
        self.kind = self.info.get("kind") or self.result.get("kind") or self._first_kind()

    def _json(self, name: str) -> Any:
        path = self.dir / name
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None

    def _first_kind(self) -> str:
        for e in self.events:
            for kind in ("review", "thread", "map"):
                if e["kind"] == f"{kind}_start":
                    return kind
        return "?"

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["kind"] == kind]

    def agents(self) -> dict[str, dict[str, Any]]:
        """Per agent run: its start/end events and its timeline (requests, responses,
        tool calls) in order."""
        agents: dict[str, dict[str, Any]] = {}
        request_run: dict[int, str] = {}
        for e in self.events:
            run = e.get("run")
            if e["kind"] == "run_start":
                agents[run] = {"start": e, "end": None, "timeline": []}
            elif run in agents and e["kind"] == "run_end":
                agents[run]["end"] = e
            elif e["kind"] == "llm_request" and run in agents:
                request_run[e["seq"]] = run
                agents[run]["timeline"].append(e)
            elif e["kind"] == "llm_response" and e.get("request_seq") in request_run:
                agents[request_run[e["request_seq"]]]["timeline"].append(e)
            elif e["kind"] in ("tool_call", "tool_error", "guard", "compaction",
                               "run_model_switch") and run in agents:
                agents[run]["timeline"].append(e)
        return agents


# --------------------------------------------------------------------------- sections


def _header(run: Run) -> str:
    job = run.info.get("job") or {}
    mr = job.get("mr") or {}
    rev = job.get("revisions") or {}
    res = run.result
    rows = [("Run", run.info.get("run_id") or res.get("run_id")),
            ("Kind", run.kind), ("Status", res.get("status")),
            ("Project", f"{mr.get('project', '?')}" + (f" !{mr['iid']}" if mr.get("iid") else "")),
            ("Title", mr.get("title")),
            ("Revisions", f"base {str(rev.get('base_sha', ''))[:10]} → head "
                          f"{str(rev.get('head_sha', ''))[:10]}"),
            ("Requests", res.get("requests")), ("Brain", run.info.get("brain_version")),
            ("Memory commit", (run.info.get("memory_commit") or res.get("memory_commit")
                               or "none")[:12]),
            ("Sandbox", run.info.get("sandbox")),
            ("Roles", ", ".join(f"{k}={v}" for k, v in (run.info.get("roles") or {}).items())),
            ("Prompts", ", ".join(f"{k}:{v}" for k, v in (run.info.get("prompts") or {}).items()))]
    body = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in rows if v)
    return f"<table>{body}</table>"


def _outcome(run: Run) -> str:
    res = run.result
    parts = []
    if res.get("reason"):
        parts.append(f"<p class='bad'>Reason: {esc(res['reason'])}</p>")
    if res.get("summary"):
        parts.append(f"<div class='box'><b>Summary</b>{pre(res['summary'])}</div>")
    for c in res.get("comments") or []:
        parts.append(f"<div class='box'><b>Comment on <code>{esc(c['path'])}:{esc(c['line'])}"
                     f"</code></b> ({esc(c.get('side'))} side){pre(c['body'])}</div>")
    thread = res.get("thread")
    if thread:
        cls = status_class(thread["verdict"])
        parts.append(f"<div class='box'><b>Verdict: <span class='{cls}'>"
                     f"{esc(thread['verdict'])}</span></b>{pre(thread.get('body'))}"
                     + (f"<p>Lesson candidate: {esc(thread['lesson_candidate'])}</p>"
                        if thread.get("lesson_candidate") else "") + "</div>")
    for u in res.get("memory_updates") or []:
        text = u.get("content") or json.dumps(u.get("lesson"), indent=1)
        parts.append(f"<details><summary>Memory update: {esc(u['kind'])} for "
                     f"{esc(u['project'])}</summary>{pre(text)}</details>")
    if not parts and run.kind == "map":
        card = run.dir / "card.md"
        if card.is_file():
            parts.append(f"<div class='box'><b>Card</b>{pre(card.read_text())}</div>")
    return "".join(parts) or "<p class='muted'>No result.json in this run.</p>"


def _funnel(run: Run) -> str:
    m = run.metrics or {}
    by_lens = m.get("by_lens") or {}
    if not by_lens:
        return ""
    head = "<tr><th>lens</th>" + "".join(f"<th>{k}</th>" for k in FUNNEL) + "</tr>"
    rows = "".join("<tr><td>" + esc(lens) + "</td>" + "".join(
        f"<td>{c.get(k, 0)}</td>" for k in FUNNEL) + "</tr>" for lens, c in by_lens.items())
    total = m.get("funnel") or {}
    rows += "<tr><th>all</th>" + "".join(f"<th>{total.get(k, 0)}</th>" for k in FUNNEL) + "</tr>"
    drops = ", ".join(f"{k}: {v}" for k, v in (m.get("dropped_at") or {}).items())
    return (f"<h2>Funnel</h2><table>{head}{rows}</table>"
            + (f"<p class='muted'>Dropped at: {esc(drops)}</p>" if drops else ""))


def _links(chain: list[Any]) -> str:
    items = []
    for link in chain:
        d = link if isinstance(link, dict) else link.model_dump()
        where = (f"{d.get('repo')}:" if d.get("repo") else "") + f"{d.get('path')}:{d.get('line')}"
        items.append(f"<li><code>{esc(where)}</code> <code>{esc(d.get('quote'))}</code> — "
                     f"{esc(d.get('claim'))}</li>")
    return f"<ul>{''.join(items)}</ul>" if items else ""


def _witnesses(witnesses: list[Any]) -> str:
    out = []
    for w in witnesses:
        d = w if isinstance(w, dict) else w.model_dump()
        out.append(f"<li>{esc(d['id'])} ({esc(d['root'])}) <code>{esc(d['cmd'])}</code> exited "
                   f"{esc(d['exit_code'])} — {esc(d.get('shows'))}</li>")
    return f"<p>Sandbox runs cited:</p><ul>{''.join(out)}</ul>" if out else ""


def _tree(run: Run, agents: dict[str, dict[str, Any]]) -> str:
    hyps: dict[str, dict[str, Any]] = {}
    for e in run.of("triage"):
        for h in e.get("hypotheses") or []:
            hyps[h["id"]] = h
    records: dict[str, list[Finding]] = defaultdict(list)
    for f in run.findings:
        records[f.hypothesis or f.id].append(f)
    ids = list(hyps) + [k for k in records if k not in hyps]
    if not ids:
        return ""
    out = ["<h2>Hypothesis tree</h2>"]
    for hid in ids:
        h = hyps.get(hid, {})
        fs = records.get(hid, [])
        final = next((f for f in fs if f.status == "posted"), fs[0] if fs else None)
        status = final.status if final else "?"
        claim = h.get("claim") or (final.claim if final else "")
        lenses = h.get("lenses") or ((final.category or "").split(",") if final else [])
        tags = "".join(f"<span class='tag'>{esc(x)}</span>" for x in lenses if x)
        title = (f"<b>{esc(hid)}</b> <span class='{status_class(status)}'>{esc(status)}</span> "
                 f"{tags} {esc(h.get('severity') or (final.severity if final else ''))} — "
                 f"{esc(claim[:180])}")
        body = []
        if h:
            body.append(f"<p>Why suspected: {esc(h.get('why'))}<br>Changed line: "
                        f"<code>{esc(h.get('changed'))}</code></p>")
        inv = f"investigate/{hid}"
        if inv in agents:
            end = agents[inv]["end"] or {}
            out_args = end.get("output") or {}
            body.append(f"<p>Investigation (<a href='#{anchor(inv)}'>{esc(inv)}</a>): "
                        f"<span class='{status_class(str(out_args.get('verdict')))}'>"
                        f"{esc(out_args.get('verdict') or end.get('status'))}</span> — "
                        f"{esc(out_args.get('reason') or end.get('error'))}</p>")
            body.append(_links(out_args.get("chain") or []))
        for f in fs:
            if f.status == "posted":
                body.append(f"<p class='ok'>Posted as <code>{esc(f.id)}</code> on "
                            f"<code>{esc(f.anchor.path)}:{esc(f.anchor.line)}</code>: "
                            f"{esc(f.title)}</p>{_links(f.chain)}{_witnesses(f.witnesses)}")
            elif f.status == "dropped":
                body.append(f"<p class='bad'>Dropped at <b>{esc(f.dropped_at)}</b>: "
                            f"{esc(f.drop_reason)}</p>")
            else:
                body.append(f"<p class='warn'>{esc(f.status)}: {esc(f.drop_reason)}</p>")
            for v in f.verifications:
                label = f"{v.verifier}/{f.id}"
                link = f"<a href='#{anchor(label)}'>{esc(v.verifier)}</a>" if label in agents \
                    else esc(v.verifier)
                body.append(f"<div class='box'>{link} ({esc(v.model)}): "
                            f"<span class='{status_class(v.outcome)}'>{esc(v.outcome)}</span>"
                            f"<br>{esc(v.reason)}{_links(v.evidence)}"
                            f"{_witnesses(v.witnesses)}</div>")
        out.append(f"<details class='box'><summary>{title}</summary>{''.join(body)}</details>")
    return "".join(out)


def _stages(run: Run) -> str:
    rows = "".join(f"<tr><td>{esc(e['stage'])}</td><td>{esc(e['requests'])}</td>"
                   f"<td>{esc(e['agents'])}</td><td>{esc(e['seconds'])}s</td>"
                   f"<td>{esc(e.get('notes'))}</td></tr>" for e in run.of("stage_end"))
    if not rows:
        return ""
    return ("<h2>Stages</h2><table><tr><th>stage</th><th>requests</th><th>agents</th>"
            f"<th>time</th><th>notes</th></tr>{rows}</table>")


def _message(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return body["choices"][0]["message"] or {}
    except (KeyError, IndexError, TypeError):
        return {}


def _agent(label: str, a: dict[str, Any]) -> str:
    start, end = a["start"], a["end"] or {}
    status = end.get("status", "running")
    head = (f"<b>{esc(label)}</b> — {esc(start.get('role'))}, {esc(start.get('model'))}, "
            f"<span class='{status_class(status)}'>{esc(status)}</span>, "
            f"{esc(end.get('turns', '?'))} turns, {esc(end.get('requests', '?'))} requests")
    parts = []
    for e in a["timeline"]:
        if e["kind"] == "llm_response":
            msg = _message(e.get("body") or {})
            reasoning = msg.get("reasoning_content") or msg.get("reasoning")
            if reasoning:
                parts.append(f"<details><summary class='muted'>reasoning</summary>"
                             f"{pre(reasoning)}</details>")
            if msg.get("content"):
                parts.append(f"<p class='muted'>said:</p>{pre(msg['content'], 4000)}")
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                parts.append(f"<p>→ <b>{esc(fn.get('name'))}</b> <code>"
                             f"{esc(str(fn.get('arguments'))[:600])}</code></p>")
        elif e["kind"] == "tool_call":
            parts.append(f"<details><summary>{esc(e['tool'])} result "
                         f"({esc(e.get('output_chars'))} chars, {esc(e.get('seconds'))}s)"
                         f"</summary>{pre(e.get('output'))}</details>")
        elif e["kind"] == "llm_request" and e.get("turn") == 1:
            msgs = (e.get("body") or {}).get("messages") or []
            if len(msgs) > 1:
                parts.append(f"<details><summary class='muted'>system prompt and task</summary>"
                             f"{pre(msgs[0].get('content'), 8000)}"
                             f"{pre(msgs[1].get('content'), 12000)}</details>")
        else:
            parts.append(f"<p class='warn'>{esc(e['kind'])}: "
                         f"{esc({k: v for k, v in e.items() if k not in ('seq', 'ts')})}</p>")
    if end.get("output") is not None:
        parts.append(f"<p><b>Concluded</b></p>{pre(json.dumps(end['output'], indent=1))}")
    if end.get("error"):
        parts.append(f"<p class='bad'>{esc(end['error'])}</p>")
    return (f"<details id='{anchor(label)}' class='box'><summary>{head}</summary>"
            f"{''.join(parts)}</details>")


def _extras(run: Run) -> str:
    out = []
    runs = run.of("sandbox_run")
    if runs:
        rows = "".join(f"<tr><td>{esc(e['id'])}</td><td>{esc(e['agent'])}</td>"
                       f"<td>{esc(e['root'])}</td><td><code>{esc(e['cmd'])}</code></td>"
                       f"<td class='{'ok' if e['exit_code'] == 0 else 'bad'}'>"
                       f"{esc(e['exit_code'])}</td><td>{esc(e['seconds'])}s</td></tr>"
                       for e in runs)
        out.append("<h2>Sandbox runs</h2><table><tr><th>id</th><th>agent</th><th>root</th>"
                   f"<th>command</th><th>exit</th><th>time</th></tr>{rows}</table>"
                   "<p class='muted'>Full output: runs/&lt;id&gt;.txt in the run directory.</p>")
    for e in run.of("team_rules"):
        if e.get("files") or e.get("dropped"):
            out.append(f"<h2>Team rules</h2><p>From: {esc(', '.join(e.get('files') or []))}</p>"
                       + (f"<p class='warn'>Dropped as suppression: "
                          f"{esc('; '.join(e['dropped']))}</p>" if e.get("dropped") else ""))
    for e in run.of("bar_raised"):
        out.append(f"<h2>Raised bars</h2>{pre(json.dumps(e.get('bars'), indent=1))}"
                   f"<p>Held back: {esc(e.get('held_back'))}</p>")
    pack = run.dir / "evidence_pack.md"
    if pack.is_file():
        out.append(f"<h2>Evidence pack</h2><details><summary>{pack.stat().st_size} bytes"
                   f"</summary>{pre(pack.read_text(), 200_000)}</details>")
    failures = [e for e in run.events if e["kind"].endswith("_failed") or e["kind"] in (
        "llm_error", "model_fallback", "degenerate_turn")]
    if failures:
        out.append("<h2>Errors and fallbacks</h2>" + "".join(
            f"<p class='bad'>{esc(e['kind'])}: "
            f"{esc({k: v for k, v in e.items() if k not in ('seq', 'ts', 'body')})}</p>"
            for e in failures))
    return "".join(out)


def render(run_dir: Path | str) -> str:
    run = Run(Path(run_dir))
    agents = run.agents()
    title = f"Habodek {run.kind} trace — {run.info.get('run_id') or run.dir.name}"
    body = [f"<h1>{esc(title)}</h1>", _header(run), "<h2>Outcome</h2>", _outcome(run),
            _funnel(run), _tree(run, agents), _stages(run)]
    if agents:
        body.append("<h2>Agents</h2>" + "".join(_agent(k, v) for k, v in agents.items()))
    body.append(_extras(run))
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width, initial-scale=1'><title>{esc(title)}</title>"
            f"<style>{CSS}</style></head><body>{''.join(body)}</body></html>\n")


def show(run_dir: Path | str, out: Path | str | None = None) -> Path:
    target = Path(out) if out else Path(run_dir) / "trace.html"
    target.write_text(render(run_dir), encoding="utf-8")
    return target
