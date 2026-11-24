#!/usr/bin/env python3
"""Habodek gateway probe: what the LiteLLM gateway and its two models really do.

Standard library only, so it runs on the VPN machine with nothing installed:

    python3 gateway_probe.py --base-url https://litellm.example/v1 --key-env LITELLM_KEY \\
        --model qwen=<qwen alias> --model glm=<glm alias>

Prints at most 20 lines. Every raw request and response (key redacted) goes to
<out>/raw.jsonl, the structured results to <out>/results.json; <out> is printed last.

It paces itself under --rpm (default 20, below the ~25 the gateway is believed to
allow) and backs off on 429. About 55 requests per model; with the soak, expect
10-25 minutes in all. The checks follow docs/reviewer-v2/survey/models.md §8.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

VERSION = "1"
EFFORTS = ("low", "medium", "high", "xhigh", "max", "minimal")
SMALL = 4096  # max_tokens for the small checks: enough to think and answer briefly

READ_FILE = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read one file of the repository and return its full text.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path of the file."}},
            "required": ["path"],
        },
    },
}
FINISH = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": "Call once, after every file has been read.",
        "parameters": {
            "type": "object",
            "properties": {
                "tokens": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The TOKEN value of every file, in reading order.",
                }
            },
            "required": ["tokens"],
        },
    },
}
SUBMIT = {
    "type": "function",
    "function": {
        "name": "submit_review",
        "description": "Submit a review comment.",
        "parameters": {
            "type": "object",
            "properties": {"body": {"type": "string"}},
            "required": ["body"],
        },
    },
}
_MARKUP = re.compile(r"<tool_call>|<function=|<\|tool_call")
_FENCE = re.compile(r"```.*?(```|$)", re.DOTALL)
_CHAR_RUN = re.compile(r"(.)\1{199,}", re.DOTALL)
_PHRASE_LOOP = re.compile(r"(.{2,40}?)(?:\s*\1){24,}\s*$", re.DOTALL)


# --------------------------------------------------------------------------- transport


class Response:
    def __init__(self, status, headers, text, elapsed):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.text = text
        self.elapsed = elapsed
        try:
            self.json = json.loads(text) if text and not text.startswith("data:") else None
        except ValueError:
            self.json = None

    @property
    def message(self):
        try:
            return (self.json or {})["choices"][0]["message"] or {}
        except (KeyError, IndexError, TypeError):
            return {}

    @property
    def finish(self):
        try:
            return self.json["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError):
            return None

    @property
    def usage(self):
        return (self.json or {}).get("usage") or {}

    def error(self):
        body = self.json if isinstance(self.json, dict) else {}
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        return str(err or self.text)[:300]


class Gateway:
    def __init__(self, base_url, key, rpm, out_dir, timeout=600.0):
        base = base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        self.v1 = base if base.endswith("/v1") else base + "/v1"
        self.root = self.v1[: -len("/v1")]
        self.key = key
        self.interval = 60.0 / rpm
        self.timeout = timeout
        self.raw = open(os.path.join(out_dir, "raw.jsonl"), "a", encoding="utf-8")
        self.lock = threading.Lock()
        self.next_slot = 0.0
        self.starts = []
        self.requests = 0
        self.throttled = 0
        self.retry_after = []
        self.rate_headers = {}
        self.stream_errors = 0

    def close(self):
        self.raw.close()

    def _pace(self):
        with self.lock:
            now = time.monotonic()
            wait = max(0.0, self.next_slot - now)
            self.next_slot = max(now, self.next_slot) + self.interval
        if wait:
            time.sleep(wait)
        with self.lock:
            self.starts.append(time.monotonic())
            self.requests += 1

    def _log(self, record):
        with self.lock:
            self.raw.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.raw.flush()

    def call(self, method, url, body=None, label="", paced=True, retries=4):
        for attempt in range(retries + 1):
            if paced:
                self._pace()
            data = json.dumps(body).encode("utf-8") if body is not None else None
            request = urllib.request.Request(url, data=data, method=method)
            request.add_header("Content-Type", "application/json")
            if self.key:
                request.add_header("Authorization", "Bearer " + self.key)
            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as handle:
                    status, headers = handle.status, dict(handle.headers.items())
                    text = handle.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                status, headers = exc.code, dict(exc.headers.items()) if exc.headers else {}
                text = exc.read().decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001 - timeouts, resets: recorded, not fatal
                status, headers, text = 0, {}, "%s: %s" % (type(exc).__name__, exc)
            response = Response(status, headers, text, time.monotonic() - started)
            for key, value in response.headers.items():
                if key.startswith("x-ratelimit") or key == "retry-after":
                    self.rate_headers[key] = value
            self._log({"label": label, "attempt": attempt, "method": method, "url": url,
                       "request": body, "status": status, "elapsed_s": round(response.elapsed, 3),
                       "headers": response.headers, "response": response.json or text[:50000]})
            if status != 429:
                return response
            self.throttled += 1
            try:
                pause = float(response.headers.get("retry-after", ""))
                self.retry_after.append(pause)
            except ValueError:
                pause = 10.0 * (attempt + 1)
            time.sleep(min(max(pause, 1.0), 120.0))
        return response

    def chat(self, body, label=""):
        return self.call("POST", self.v1 + "/chat/completions", body, label)

    def rpm_observed(self):
        starts, best = sorted(self.starts), 0
        j = 0
        for i, t in enumerate(starts):
            while starts[j] < t - 60.0:
                j += 1
            best = max(best, i - j + 1)
        if starts and starts[-1] - starts[0] < 60.0:
            span = max(starts[-1] - starts[0], 1.0)
            return round(len(starts) * 60.0 / span, 1) if len(starts) > 1 else 0.0
        return float(best)


# --------------------------------------------------------------------------- helpers


def reasoning_of(message):
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return key, value
    content = message.get("content") or ""
    if "</think>" in content:
        return "inline_think", content.split("</think>")[0].replace("<think>", "")
    return "none", ""


def content_of(message):
    content = message.get("content") or ""
    if "</think>" in content:
        content = content.split("</think>", 1)[1]
    return content.strip()


def repetitive(text):
    tail = (text or "")[-4000:]
    if _CHAR_RUN.search(tail):
        return True
    loop = _PHRASE_LOOP.search(tail)
    return bool(loop and re.search(r"\w", loop.group(1)))


def parse_args_json(call):
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def short(text, n=40):
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= n else text[: n - 1] + "~"


def body(alias, messages, **extra):
    request = {"model": alias, "messages": messages, "max_tokens": SMALL}
    request.update(extra)
    return request


def user(text):
    return [{"role": "user", "content": text}]


# --------------------------------------------------------------------------- checks


def check_info(gw, alias, res):
    models = gw.call("GET", gw.v1 + "/models", label="models", paced=False)
    ids = [m.get("id") for m in ((models.json or {}).get("data") or []) if isinstance(m, dict)]
    res["models_status"], res["alias_listed"] = models.status, alias in ids
    res["models_listed"] = ids[:50]
    info = gw.call("GET", gw.root + "/model/info", label="model_info", paced=False)
    if info.status == 404:
        info = gw.call("GET", gw.v1 + "/model/info", label="model_info", paced=False)
    res["info_status"] = info.status
    for entry in (info.json or {}).get("data") or []:
        if not isinstance(entry, dict) or entry.get("model_name") != alias:
            continue
        model_info = entry.get("model_info") or {}
        params = entry.get("litellm_params") or {}
        res["max_input_tokens"] = model_info.get("max_input_tokens")
        res["max_output_tokens"] = model_info.get("max_output_tokens")
        res["backend"] = str(params.get("model", ""))
        res["supports"] = {k: v for k, v in model_info.items() if k.startswith("supports_")}
        break


def check_reasoning(gw, alias, res):
    r = gw.chat(body(alias, user("What is 17 * 23? Answer with the number only.")),
                "reasoning")
    where, text = reasoning_of(r.message)
    details = r.usage.get("completion_tokens_details") or {}
    res["reasoning"] = {"status": r.status, "field": where, "chars": len(text),
                        "answer": short(content_of(r.message)), "finish": r.finish,
                        "reasoning_tokens": details.get("reasoning_tokens"),
                        "error": r.error() if r.status != 200 else ""}
    s = gw.chat(body(alias, user("What is 12 * 12? Answer with the number only."),
                     stream=True), "stream")
    fields, saw_error = set(), False
    for line in s.text.splitlines():
        if not line.startswith("data:") or line.strip() == "data: [DONE]":
            continue
        try:
            event = json.loads(line[5:])
        except ValueError:
            continue
        if event.get("error"):
            saw_error = True
        for choice in event.get("choices") or []:
            for key, value in (choice.get("delta") or {}).items():
                if value and key in ("reasoning_content", "reasoning", "content", "thinking"):
                    fields.add(key)
    gw.stream_errors += int(saw_error)
    res["stream"] = {"status": s.status, "fields": sorted(fields), "error_payload": saw_error}


def check_effort(gw, alias, res):
    out = {}
    for effort in EFFORTS:
        r = gw.chat(body(alias, user("Reply with OK."), reasoning_effort=effort),
                    "effort_" + effort)
        out[effort] = {"status": r.status, "completion_tokens": r.usage.get("completion_tokens"),
                       "error": short(r.error(), 120) if r.status != 200 else ""}
    res["effort"] = out
    r = gw.chat(body(alias, user("Reply with OK."),
                     chat_template_kwargs={"enable_thinking": False}), "thinking_off")
    res["thinking_off"] = {"status": r.status, "reasoning": reasoning_of(r.message)[0]}


def check_echo(gw, alias, res):
    word = "PELICAN-%d" % random.randint(100, 999)
    question = ("Earlier, in your private reasoning, you noted a secret code word. "
                "What was it? Answer with the code word only.")
    def convo(field):
        assistant = {"role": "assistant", "content": "OK."}
        if field:
            assistant[field] = "Note to self: the secret code word is %s." % word
        return [{"role": "user", "content": "Say OK."}, assistant,
                {"role": "user", "content": question}]
    with_echo = gw.chat(body(alias, convo("reasoning_content")), "echo")
    without = gw.chat(body(alias, convo(None)), "echo_none")
    alt = gw.chat(body(alias, convo("reasoning")), "echo_reasoning_field")
    delta = None
    if with_echo.status == 200 and without.status == 200:
        delta = (with_echo.usage.get("prompt_tokens") or 0) - (without.usage.get("prompt_tokens")
                                                                or 0)
    res["echo"] = {
        "status": with_echo.status, "recall": word in content_of(with_echo.message),
        "prompt_token_delta": delta, "alt_status": alt.status,
        "alt_recall": word in content_of(alt.message),
        "error": short(with_echo.error(), 120) if with_echo.status != 200 else "",
    }


def check_shapes(gw, alias, res):
    late = gw.chat(body(alias, [{"role": "user", "content": "Hi"},
                                {"role": "assistant", "content": "Hello."},
                                {"role": "system", "content": "Be terse."},
                                {"role": "user", "content": "Say OK."}]), "late_system")
    res["late_system"] = {"status": late.status,
                          "error": short(late.error(), 120) if late.status != 200 else ""}
    out = {}
    for label, value in (("null", None), ("empty", "")):
        messages = [
            {"role": "user", "content": "Read a.py and tell me its first word."},
            {"role": "assistant", "content": value, "tool_calls": [{
                "id": "call_a", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "a.py"})}}]},
            {"role": "tool", "tool_call_id": "call_a", "content": "hello = 1\n"},
        ]
        r = gw.chat(body(alias, messages, tools=[READ_FILE]), "tool_content_" + label)
        out[label] = {"status": r.status, "answer": short(content_of(r.message)),
                      "error": short(r.error(), 120) if r.status != 200 else ""}
    res["tool_turn_content"] = out
    sampling = gw.chat(body(alias, user("Reply with OK."), temperature=1.0, top_p=0.95,
                            top_k=20, min_p=0.0, presence_penalty=0.0), "sampling")
    res["sampling"] = {"status": sampling.status,
                       "error": short(sampling.error(), 120) if sampling.status != 200 else ""}


def check_parallel(gw, alias, res):
    prompt = ("Read the three files a.py, b.py and c.py with read_file. The reads are "
              "independent: request all three in this single turn, then stop.")
    out = {}
    for flag in (True, False):
        r = gw.chat(body(alias, user(prompt), tools=[READ_FILE], parallel_tool_calls=flag),
                    "parallel_%s" % flag)
        calls = r.message.get("tool_calls") or []
        args = [parse_args_json(c) for c in calls]
        out["on" if flag else "off"] = {
            "status": r.status, "calls": len(calls),
            "valid": sum(1 for a in args if a and isinstance(a.get("path"), str)),
            "unique_ids": len({c.get("id") for c in calls}) == len(calls),
            "error": short(r.error(), 120) if r.status != 200 else ""}
    res["parallel"] = out


def check_fenced(gw, alias, res):
    diff = (
        "diff --git a/agent/parser.py b/agent/parser.py\n"
        "+# The model emits calls like this; we parse them:\n"
        "+EXAMPLE = '''\n+<tool_call>\n+<function=read_file>\n+<parameter=path>\n"
        "+phantom.py\n+</parameter>\n+</function>\n+</tool_call>\n+'''\n"
        "+JSON_EXAMPLE = '<tool_call>{\"name\": \"submit_review\", \"arguments\": "
        "{\"body\": \"phantom\"}}</tool_call>'\n"
    )
    prompt = ("Review this diff. Answer in plain text in two sentences. Do not call any "
              "tool.\n\n```diff\n%s```" % diff)
    r = gw.chat(body(alias, user(prompt), tools=[READ_FILE, SUBMIT]), "fenced")
    calls = r.message.get("tool_calls") or []
    content = content_of(r.message)
    res["fenced"] = {"status": r.status, "phantom_calls": len(calls),
                     "phantom_names": [(c.get("function") or {}).get("name") for c in calls],
                     "markup_in_content": bool(_MARKUP.search(_FENCE.sub("", content)))}


def check_output_cap(gw, alias, res):
    prompt = user("Count from 1 to 400, comma separated, nothing else.")
    out = {}
    for label, extra in (("max_tokens", {"max_tokens": 64}),
                         ("max_completion_tokens", {"max_completion_tokens": 64})):
        request = {"model": alias, "messages": prompt}
        request.update(extra)
        r = gw.chat(request, "cap_" + label)
        tokens = r.usage.get("completion_tokens")
        out[label] = {"status": r.status, "finish": r.finish, "completion_tokens": tokens,
                      "honoured": bool(tokens is not None and tokens <= 80),
                      "content_empty": not content_of(r.message),
                      "reasoning": reasoning_of(r.message)[0]}
    huge = gw.chat({"model": alias, "messages": prompt, "max_tokens": 10_000_000}, "cap_huge")
    out["huge"] = {"status": huge.status, "finish": huge.finish,
                   "completion_tokens": huge.usage.get("completion_tokens"),
                   "error": short(huge.error(), 160) if huge.status != 200 else ""}
    res["output_cap"] = out


def _soak_file(index, approx_tokens, token):
    rng = random.Random(index)
    lines = ['"""Module part_%02d: storage helpers for the orders service."""' % index, ""]
    target_chars = approx_tokens * 4
    n = 0
    while sum(len(x) + 1 for x in lines) < target_chars:
        n += 1
        name = "helper_%02d_%03d" % (index, n)
        lines += [
            "def %s(store, key, retries=%d):" % (name, rng.randint(1, 5)),
            "    \"\"\"Fetch %s from the store, retrying on timeouts.\"\"\"" % key_name(rng),
            "    for attempt in range(retries):",
            "        value = store.get(key + '_%d')" % rng.randint(0, 999),
            "        if value is not None:",
            "            return value * %d" % rng.randint(2, 9),
            "    raise KeyError(key)",
            "",
        ]
        if n == 5:
            lines.append("# TOKEN: %s" % token)
            lines.append("")
    return "\n".join(lines)


def key_name(rng):
    return rng.choice(["order", "user", "invoice", "refund", "session", "cart", "payment"])


def check_soak(gw, alias, res, turns, target_tokens):
    per_file = max(500, target_tokens // max(turns, 1))
    words = ["amber", "basil", "cedar", "delta", "ember", "fjord", "gamma", "harbor", "iris",
             "jade", "kelp", "lumen", "maple", "nova", "onyx", "pearl", "quartz", "raven",
             "sable", "tundra", "umber", "vivid", "willow", "xenon", "yarrow", "zephyr"]
    tokens = {}
    for i in range(1, turns + 1):
        tokens["part_%02d.py" % i] = "%s%d" % (words[(i * 7) % len(words)], i)
    system = ("You are auditing a repository. Files part_01.py to part_%02d.py must each be "
              "read with read_file, one file per turn, in order. Each file contains a line "
              "'# TOKEN: <value>'. After the last file, call finish with every TOKEN value in "
              "order. Do not answer in plain text; always call a tool." % turns)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": "Start with part_01.py."}]
    stats = {"turns": 0, "malformed": 0, "unknown_tool": 0, "degenerate": 0, "no_call": 0,
             "markup_in_content": 0, "duplicate_calls": 0, "errors": 0, "max_prompt_tokens": 0,
             "latency": [], "finish_recall": None, "finish_reasons": {}, "stopped": ""}
    seen_calls, read = set(), []
    for _ in range(turns + 6):
        r = gw.chat({"model": alias, "messages": messages, "tools": [READ_FILE, FINISH],
                     "max_tokens": 16384}, "soak_turn_%d" % (stats["turns"] + 1))
        stats["turns"] += 1
        stats["latency"].append(round(r.elapsed, 2))
        if r.status != 200:
            stats["errors"] += 1
            stats["stopped"] = "http %d: %s" % (r.status, short(r.error(), 80))
            break
        stats["max_prompt_tokens"] = max(stats["max_prompt_tokens"],
                                         r.usage.get("prompt_tokens") or 0)
        stats["finish_reasons"][str(r.finish)] = stats["finish_reasons"].get(str(r.finish), 0) + 1
        message = r.message
        where, reasoning = reasoning_of(message)
        content = content_of(message)
        calls = message.get("tool_calls") or []
        if (r.finish == "length" and not content and not calls) or repetitive(reasoning):
            stats["degenerate"] += 1
        assistant = {"role": "assistant", "content": message.get("content") or ""}
        if reasoning and where != "inline_think":
            assistant["reasoning_content"] = reasoning
        if not calls:
            stats["no_call"] += 1
            if _MARKUP.search(_FENCE.sub("", content)):
                stats["markup_in_content"] += 1
            messages += [assistant, {"role": "user", "content":
                                     "Continue: call read_file for the next file, or finish."}]
            continue
        fixed_calls, results, finished = [], [], None
        for c in calls:
            name = (c.get("function") or {}).get("name")
            args = parse_args_json(c)
            key = json.dumps([name, args], sort_keys=True)
            if key in seen_calls:
                stats["duplicate_calls"] += 1
            seen_calls.add(key)
            call_id = c.get("id") or "call_%d_%d" % (stats["turns"], len(fixed_calls))
            fixed_calls.append({"id": call_id, "type": "function", "function": {
                "name": name or "", "arguments": json.dumps(args or {})}})
            if name not in ("read_file", "finish"):
                stats["unknown_tool"] += 1
                results.append((call_id, "error: unknown tool"))
            elif args is None or (name == "read_file" and not isinstance(args.get("path"), str)) \
                    or (name == "finish" and not isinstance(args.get("tokens"), list)):
                stats["malformed"] += 1
                results.append((call_id, "error: invalid arguments"))
            elif name == "finish":
                finished = args.get("tokens")
                results.append((call_id, "ok"))
            else:
                path = args["path"].strip().lstrip("./")
                if path in tokens:
                    read.append(path)
                    results.append((call_id, _soak_file(int(path[5:7]), per_file, tokens[path])))
                else:
                    results.append((call_id, "error: no such file %s" % path))
        assistant["tool_calls"] = fixed_calls
        messages.append(assistant)
        for call_id, text in results:
            messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
        if finished is not None:
            expected = [tokens[p] for p in dict.fromkeys(read)]
            got = [str(t).strip() for t in finished]
            stats["finish_recall"] = "%d/%d" % (sum(1 for t in expected if t in got),
                                                len(expected))
            stats["stopped"] = "finish"
            break
        if stats["max_prompt_tokens"] >= target_tokens * 1.15:
            stats["stopped"] = "token target"
            break
    else:
        stats["stopped"] = stats["stopped"] or "turn cap"
    stats["files_read"] = len(set(read))
    latency = stats.pop("latency")
    stats["latency_p50"] = round(statistics.median(latency), 1) if latency else None
    stats["latency_max"] = round(max(latency), 1) if latency else None
    res["soak"] = stats


CHECKS = ("info", "reasoning", "effort", "echo", "shapes", "parallel", "fenced", "cap", "soak")


# --------------------------------------------------------------------------- summary


def _st(status):
    return str(status) if status else "ERR"


def summarise(key, res):
    lines = []
    info = "alias=%s in=%s out=%s backend=%s" % (
        "listed" if res.get("alias_listed") else "NOT-LISTED", res.get("max_input_tokens", "?"),
        res.get("max_output_tokens", "?"), short(res.get("backend", "?"), 30))
    r = res.get("reasoning") or {}
    s = res.get("stream") or {}
    lines.append("%-5s %s reason=%s/%s rtok=%s stream=%s%s" % (
        key, info, _st(r.get("status")), r.get("field", "?"),
        "y" if r.get("reasoning_tokens") is not None else "n", _st(s.get("status")),
        "/" + ",".join(f[:9] for f in s.get("fields", [])) + ("/ERRPAYLOAD" if
                                                              s.get("error_payload") else "")))
    eff = res.get("effort") or {}
    ok = [e for e, v in eff.items() if v.get("status") == 200]
    rej = ["%s:%s" % (e, v.get("status")) for e, v in eff.items() if v.get("status") != 200]
    th = res.get("thinking_off") or {}
    echo = res.get("echo") or {}
    lines.append("%-5s effort ok=%s rej=%s think_off=%s/%s echo=%s recall=%s d=%s alt=%s/%s" % (
        key, ",".join(ok) or "-", ",".join(rej) or "-", _st(th.get("status")),
        th.get("reasoning", "?"), _st(echo.get("status")), "y" if echo.get("recall") else "n",
        echo.get("prompt_token_delta"), _st(echo.get("alt_status")),
        "y" if echo.get("alt_recall") else "n"))
    late = res.get("late_system") or {}
    ttc = res.get("tool_turn_content") or {}
    par = res.get("parallel") or {}
    on, off = par.get("on") or {}, par.get("off") or {}
    fen = res.get("fenced") or {}
    lines.append("%-5s late_sys=%s null=%s empty=%s sampling=%s parallel=%s/%s valid%s off=%s "
                 "fenced=%s" % (
                     key, _st(late.get("status")), _st((ttc.get("null") or {}).get("status")),
                     _st((ttc.get("empty") or {}).get("status")),
                     _st((res.get("sampling") or {}).get("status")), on.get("calls", "?"),
                     on.get("valid", "?"), "" if on.get("unique_ids", True) else " DUP-IDS",
                     off.get("calls", "?"),
                     "clean" if not fen.get("phantom_calls") and not fen.get("markup_in_content")
                     else "PHANTOM:%s" % fen.get("phantom_names")))
    cap = res.get("output_cap") or {}
    mt, mct, huge = cap.get("max_tokens") or {}, cap.get("max_completion_tokens") or {}, \
        cap.get("huge") or {}
    soak = res.get("soak") or {}
    lines.append("%-5s cap mt=%s mct=%s empty_at_cap=%s huge=%s | soak t=%s ctx=%sk read=%s "
                 "mal=%s deg=%s nocall=%s markup=%s dup=%s recall=%s p50=%ss stop=%s" % (
                     key, "ok" if mt.get("honoured") else mt.get("status"),
                     "ok" if mct.get("honoured") else mct.get("status"),
                     "y" if mt.get("content_empty") else "n", _st(huge.get("status")),
                     soak.get("turns", "-"), (soak.get("max_prompt_tokens") or 0) // 1000,
                     soak.get("files_read", "-"), soak.get("malformed", "-"),
                     soak.get("degenerate", "-"), soak.get("no_call", "-"),
                     soak.get("markup_in_content", "-"), soak.get("duplicate_calls", "-"),
                     soak.get("finish_recall", "-"), soak.get("latency_p50", "-"),
                     short(soak.get("stopped", "-"), 24)))
    return lines


# --------------------------------------------------------------------------- main


def main(argv=None):
    parser = argparse.ArgumentParser(description="Probe the LiteLLM gateway for Habodek.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key-env", default="LITELLM_API_KEY",
                        help="name of the environment variable holding the key")
    parser.add_argument("--model", action="append", required=True,
                        help="short=alias, e.g. qwen=qwen3.8-flash-next (repeatable)")
    parser.add_argument("--rpm", type=float, default=20.0)
    parser.add_argument("--out", default=None)
    parser.add_argument("--only", default=",".join(CHECKS))
    parser.add_argument("--soak-turns", type=int, default=30)
    parser.add_argument("--soak-tokens", type=int, default=150000)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)

    models = []
    for spec in args.model:
        key, _, alias = spec.partition("=")
        models.append((key, alias or key) if alias else (key[:5], key))
    out = os.path.abspath(args.out or "habodek-probe-" + time.strftime("%Y%m%dT%H%M%S"))
    os.makedirs(out, exist_ok=True)
    key = os.environ.get(args.key_env, "")
    gw = Gateway(args.base_url, key, args.rpm, out, timeout=args.timeout)
    only = {c.strip() for c in args.only.split(",") if c.strip()}
    started = time.time()
    results = {"version": VERSION, "base_url": gw.v1, "rpm_cap": args.rpm,
               "key_sha8": hashlib.sha256(key.encode()).hexdigest()[:8] if key else "",
               "models": {}}
    failures = []
    for short_name, alias in models:
        res = results["models"].setdefault(short_name, {"alias": alias})
        steps = [("info", check_info, ()), ("reasoning", check_reasoning, ()),
                 ("effort", check_effort, ()), ("echo", check_echo, ()),
                 ("shapes", check_shapes, ()), ("parallel", check_parallel, ()),
                 ("fenced", check_fenced, ()), ("cap", check_output_cap, ()),
                 ("soak", check_soak, (args.soak_turns, args.soak_tokens))]
        for name, step, extra in steps:
            if name not in only:
                continue
            try:
                step(gw, alias, res, *extra)
            except Exception as exc:  # noqa: BLE001 - one broken check must not end the probe
                res.setdefault("crashed", {})[name] = "%s: %s" % (type(exc).__name__, exc)
                failures.append("%s.%s %s" % (short_name, name, short(exc, 50)))
        with open(os.path.join(out, "results.json"), "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=2, ensure_ascii=False)
    elapsed = time.time() - started
    results["rate"] = {"requests": gw.requests, "throttled": gw.throttled,
                       "retry_after": gw.retry_after[:20], "headers": gw.rate_headers,
                       "rpm_observed": gw.rpm_observed(), "stream_error_payloads": gw.stream_errors,
                       "elapsed_s": round(elapsed)}
    with open(os.path.join(out, "results.json"), "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, ensure_ascii=False)
    gw.close()

    lines = ["habodek gateway probe v%s %s base=%s rpm_cap=%g requests=%d %dm%02ds" % (
        VERSION, time.strftime("%Y-%m-%dT%H:%M"), short(gw.v1, 48), args.rpm, gw.requests,
        elapsed // 60, elapsed % 60)]
    for short_name, _ in models:
        lines += summarise(short_name, results["models"][short_name])
    rate = results["rate"]
    lines.append("rate 429=%d retry_after=%s headers=%s rpm_obs=%s" % (
        rate["throttled"], ",".join("%g" % x for x in rate["retry_after"][:4]) or "-",
        short(",".join("%s=%s" % (k.replace("x-ratelimit-", "rl-"), v)
                       for k, v in sorted(rate["headers"].items())) or "-", 90),
        rate["rpm_observed"]))
    lines.append("crashed: " + ("; ".join(failures)[:150] if failures else "none"))
    lines.append("details: " + out)
    print("\n".join(lines[:20]))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
