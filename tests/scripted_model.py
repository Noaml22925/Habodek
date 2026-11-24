"""A scripted model for the review and thread workflows on the scenario fixtures.

It plays each role the way a competent model would: it makes real tool calls first
(so the code-intelligence tools run on the scenario repositories) and then calls the
role's terminal tool with arguments built from the actual code. Quotes are copied from
the files; line numbers come from them too.

`mode` switches in the failure behaviours the mechanical checks must stop.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from eval.scenarios.build import resolve_line
from habodek.brain.llm import fake_gateway as fg


class ScriptedModel:
    def __init__(self, built: Path, mode: str = "good") -> None:
        self.built = built
        self.mode = mode
        self.calls: list[str] = []  # role per request, for assertions

    # ------------------------------------------------------------------ helpers
    def _line(self, repo: str, ref: str, path: str, text: str) -> int:
        return resolve_line(self.built / "repos" / repo, ref, path, text)

    def _link(self, repo: str, ref: str, path: str, text: str, claim: str,
              spec_prefix: str = "") -> dict:
        line = self._line(repo, ref, path, text)
        return {"path": spec_prefix + path, "line": line, "quote": text, "claim": claim}

    @staticmethod
    def _scenario(task: str) -> str:
        for sid, marker in (("1", "Return None from get_user"), ("2", "Bump payments-lib"),
                            ("3", "Move quantity validation"), ("4", "Admin: list all refunds")):
            if marker in task:
                return sid
        return "?"

    # ------------------------------------------------------------------ dispatch
    def __call__(self, body: dict) -> fg.Reply:
        messages = body["messages"]
        system = messages[0]["content"]
        task = messages[1]["content"]
        role = system.splitlines()[0].removeprefix("Role: ").strip()
        step = sum(1 for m in messages if m["role"] == "assistant")
        tools = {t["function"]["name"] for t in body.get("tools") or []}
        self.calls.append(role)
        self.tools = tools
        sid = self._scenario(task)
        if tools == {"record_progress"}:
            return fg.call("record_progress", established=[], hypotheses=[], open_questions=[],
                           files_read=[], next_step="conclude")
        if role == "understand":
            return self._understand(step, sid)
        if role.startswith("finder"):
            lens = re.search(r"\((\w+) lens\)", role).group(1)
            return self._finder(step, sid, lens)
        if role == "investigator":
            return self._investigator(step, sid, task, messages)
        if role == "verifier":
            return self._verifier(step, sid, task)
        if role == "thread":
            return self._thread(step, task, messages)
        if role == "thread challenger":
            return self._challenger(step, task)
        if role == "judge":
            ids = re.findall(r"^Finding (\S+) \(", task, re.MULTILINE)
            return fg.call("decide", post=ids, drop=[],
                           summary="One blocking issue found; see the inline comment.")
        raise AssertionError(f"unscripted role {role!r}")

    # ------------------------------------------------------------------ roles
    def _understand(self, step: int, sid: str) -> fg.Reply:
        if step == 0:
            return fg.call("glob", pattern="**/*.py", reasoning="orient first")
        return fg.call("record_understanding", purpose="Understood the change from the diff "
                       "and the changed functions.", contracts=[], unmentioned=[])

    def _finder(self, step: int, sid: str, lens: str) -> fg.Reply:
        if step == 0:
            if lens == "history":
                return fg.call("git_log", max_count=5)
            symbol = {"1": "get_user", "2": "PaymentDeclined", "3": "validate_quantity",
                      "4": "AdminBlueprint"}.get(sid, "main")
            return fg.calls(("references", {"name": symbol}), ("read", {"paths": ["."]}),
                            reasoning=f"walk the {lens} lens")
        hypotheses = []
        if sid == "1" and lens in ("intent", "consumers"):
            hypotheses.append({
                "claim": "get_user now returns None for a missing user; callers that "
                         "dereference the result or catch NotFound break",
                "why": "the raise NotFound was replaced", "severity": "major",
                "where": ["platform_common/cache/users.py", "repo:orders-service:orders"],
                "refute_by": "every caller handles None",
                "changed_line": {"path": "platform_common/store/users.py",
                                 "code": "return None"}})
        if sid == "2" and lens == "consumers":
            hypotheses.append({
                "claim": "payments-lib 1.5.0 returns declined charges instead of raising "
                         "PaymentDeclined, so checkout marks declined orders paid",
                "why": "library diff changes charge()", "severity": "critical",
                "where": ["orders/domain/checkout.py"],
                "changed_line": {"path": "requirements.txt", "code": "payments-lib==1.5.0"}})
        if sid == "3" and lens == "correctness":
            hypotheses.append({
                "claim": "validate_quantity does not handle a None quantity",
                "why": "no None check before the comparison", "severity": "minor",
                "changed_line": {"path": "orders/validation.py",
                                 "code": "if quantity <= 0 or quantity > MAX_QUANTITY:"}})
        if sid == "4" and lens == "security":
            hypotheses.append({
                "claim": "the new /admin/refunds handler has no authorisation check",
                "why": "the handler does not check the user", "severity": "critical",
                "changed_line": {"path": "orders/api/admin.py",
                                 "code": '@admin.route("GET", "/refunds")'}})
        return fg.call("propose_hypotheses", receipt=f"walked the {lens} lens: read the "
                       "changed files and the references of the changed symbols",
                       hypotheses=hypotheses)

    def _investigator(self, step: int, sid: str, task: str, messages: list) -> fg.Reply:
        if step == 0:
            target = {"1": "get_user", "2": "PaymentDeclined", "3": "validate_quantity",
                      "4": "AdminBlueprint"}[sid]
            return fg.calls(("references", {"name": target}), ("note", {"text": "walked"}),
                            reasoning="find every site")
        if sid == "1":
            return self._conclude_s1(step)
        witnesses = []
        if sid == "2" and "run" in self.tools:
            ran = _tool_text(messages)
            if "run1 (" not in ran:
                return fg.calls(("run", {"root": "head",
                                         "cmd": "python -m pytest -q tests/test_checkout.py"}),
                                ("run", {"root": "base",
                                         "cmd": "python -m pytest -q tests/test_checkout.py"}),
                                reasoning="the repo has a test for declined payments: run it")
            head = re.search(r"(run\d+) \(head\): exit (\d+)", ran)
            base = re.search(r"(run\d+) \(base\): exit (\d+)", ran)
            if self.mode == "fake_run":
                witnesses = [{"run": "run99", "shows": "invented"}]
            elif head and base and head.group(2) != "0" and base.group(2) == "0":
                witnesses = [{"run": head.group(1), "shows": "test_declined_cancels fails "
                              "with payments-lib 1.5.0: the order ends up paid"},
                             {"run": base.group(1), "shows": "the same test passes with 1.4.2"}]
        if sid == "2":
            ref = "mr/s2-payments-1.5.0"
            chain = [
                self._link("orders-service", ref, "requirements.txt", "payments-lib==1.5.0",
                           "the bump"),
                {"path": "dep:payments-lib@1.5.0:payments/client.py", "line": 32,
                 "quote": 'return ChargeResult(response.get("id", ""), "declined", '
                          'amount_cents, currency,',
                 "claim": "1.5.0 returns a declined result instead of raising"},
                self._link("orders-service", ref, "orders/domain/checkout.py",
                           "except PaymentDeclined as exc:", "the only decline handling"),
                self._link("orders-service", ref, "orders/domain/checkout.py",
                           'order.status = "paid"', "declined charges fall through to paid"),
            ]
            return fg.call("conclude", verdict="confirmed", reason="traced the library change",
                           title="payments-lib 1.5.0 returns declined charges; checkout marks "
                                 "them paid", severity="critical",
                           claim="charge() no longer raises PaymentDeclined.",
                           scenario="A declined card: the order is marked paid and shipped.",
                           fix="Check result.succeeded.",
                           changed_line={"path": "requirements.txt",
                                         "code": "payments-lib==1.5.0"}, chain=chain,
                           **({"witnesses": witnesses} if witnesses else {}))
        if sid == "3":
            return fg.call("conclude", verdict="refuted", reason="orders/schema.py:22 _int "
                           "rejects anything not an int before every call site")
        return fg.call("conclude", verdict="refuted", reason="platform_common/web/admin.py:20 "
                       "AdminBlueprint registers require_user and require_admin on every route")

    def _conclude_s1(self, step: int) -> fg.Reply:
        ref = "mr/s1-get-user-none"
        chain = [
            self._link("platform-common", ref, "platform_common/store/users.py", "return None",
                       "returns None where it raised"),
            self._link("platform-common", ref, "platform_common/cache/users.py",
                       "except NotFound:", "misses no longer negatively cached"),
            self._link("orders-service", "main", "orders/api/profile.py",
                       'return Response(200, {"email": user.email, "name": user.name})',
                       "AttributeError, 500", "repo:orders-service:"),
            self._link("orders-service", "main", "orders/jobs/digest.py",
                       "notify.send_digest(user.email, orders_store.list_for_user(uid))",
                       "digest dies", "repo:orders-service:"),
        ]
        changed = {"path": "platform_common/store/users.py", "code": "return None"}
        if self.mode == "invent_quote":
            chain[2] = {**chain[2], "quote": "profile = user.get_email()"}
        elif self.mode == "off_diff":
            changed = {"path": "orders/api/profile.py", "code": "user = get_user(request.user_id)"}
        elif self.mode == "no_evidence":
            chain = chain[:1]
        return fg.call("conclude", verdict="confirmed", reason="traced every caller",
                       title="get_user() now returns None for a missing user, and callers "
                             "dereference it", severity="major",
                       claim="get_user returns None instead of raising NotFound.",
                       scenario="A deleted user opens their profile: 500 instead of 404.",
                       fix="Keep raising NotFound.", changed_line=changed, chain=chain)

    def _verifier(self, step: int, sid: str, task: str) -> fg.Reply:
        if step == 0:
            return fg.call("read", paths=["."], reasoning="check the chain")
        if self.mode == "verifier_refutes":
            return fg.call("verdict", outcome="refuted", reason="the profile route is guarded",
                           evidence=[{"path": "repo:orders-service:orders/api/profile.py",
                                      "line": 14, "quote": "if request.user_id is None:",
                                      "claim": "guard"}])
        return fg.call("verdict", outcome="upheld", reason="every link checks out and nothing "
                       "upstream prevents it")


def _tool_text(messages: list) -> str:
    return "\n".join(m.get("content") or "" for m in messages if m["role"] == "tool")


def _line_of(text: str, path_header: str, needle: str) -> int | None:
    """Line number of `needle` in a `read` result block for `path_header`."""
    block = text.split(f"== {path_header} ", 1)
    if len(block) < 2:
        return None
    for row in block[1].split("\n== ", 1)[0].splitlines()[1:]:
        number, _, code = row.partition("\t")
        if needle in code:
            return int(number)
    return None


def _thread_methods():
    def _thread(self, step: int, task: str, messages: list) -> fg.Reply:
        f2 = "# Your finding F2" in task
        if "payments-lib" in task.split("# The thread")[0]:
            return self._thread_payments(step, messages)
        if step == 0:
            paths = (["orders/app.py", "orders/api/refunds.py", "orders/middleware/ownership.py"]
                     if f2 else ["platform_common/store/users.py",
                                 "platform_common/cache/users.py",
                                 "repo:orders-service:orders/api/profile.py",
                                 "repo:orders-service:orders/jobs/digest.py"])
            return fg.call("read", paths=paths, reasoning="re-walk the chain at head")
        seen = _tool_text(messages)
        if f2:
            return self._thread_f2(seen, task)
        return self._thread_f1(seen)

    def _thread_f1(self, seen: str) -> fg.Reply:
        users = "platform_common/store/users.py"
        if 'raise NotFound(f"user {uid}")' in seen.split("== platform_common/cache")[0] and \
                "return None" not in seen.split("== platform_common/cache")[0]:
            return fg.call("conclude_thread", claim_type="none", verdict="fixed",
                           reason="get_user raises NotFound again; every site recovers",
                           reply="Fixed: get_user raises NotFound again "
                                 f"({users}:{_line_of(seen, users, 'raise NotFound')}).")
        digest = "repo:orders-service:orders/jobs/digest.py"
        digest_line = _line_of(seen, digest, "user.email")
        cache_line = _line_of(seen, "platform_common/cache/users.py", "except NotFound:")
        digest_link = {"path": digest, "line": digest_line, "claim": "still dereferences None",
                       "quote": "notify.send_digest(user.email, orders_store.list_for_user(uid))"}
        cache_link = {"path": "platform_common/cache/users.py", "line": cache_line,
                      "quote": "except NotFound:", "claim": "misses still not cached"}
        profile = "repo:orders-service:orders/api/profile.py"
        if _line_of(seen, profile, "if user is None:") is not None:
            return fg.call("conclude_thread", claim_type="handled_elsewhere",
                           verdict="fix_incomplete", reason="profile handles None; the digest "
                           "and the cache do not",
                           reply="Partly fixed: the profile endpoint now handles None. The "
                                 "weekly digest and the user cache are still affected.",
                           remaining=[digest_link, cache_link])
        users_line = _line_of(seen, users, "return None")
        return fg.call("conclude_thread", claim_type="fixed", verdict="stands",
                       reason="the push does not touch get_user or any site in the chain",
                       reply="This still applies: get_user still returns None.",
                       remaining=[{"path": users, "line": users_line, "quote": "return None",
                                   "claim": "unchanged"}, digest_link])

    def _thread_f2(self, seen: str, task: str) -> fg.Reply:
        if '"/api/v1/refunds", owns_refund' in seen:
            line = _line_of(seen, "orders/app.py", '"/api/v1/refunds", owns_refund')
            return fg.call("conclude_thread", claim_type="handled_elsewhere",
                           verdict="withdrawn", reason="owns_refund covers the v1 prefix",
                           reply="You're right, withdrawn: owns_refund is registered on "
                                 "/api/v1/refunds.",
                           convinced_by=[{"path": "orders/app.py", "line": line,
                                          "quote": 'router.use_prefix("/api/v1/refunds", '
                                                   'owns_refund)', "claim": "covers v1"}],
                           lesson="Ownership checks can be prefix middleware in orders/app.py.")
        v2 = _line_of(seen, "orders/app.py", '"/api/v2/refunds", owns_refund')
        reg = _line_of(seen, "orders/app.py", "router.register(refunds.bp)")
        return fg.call("conclude_thread", claim_type="handled_elsewhere", verdict="stands",
                       reason="owns_refund is only on /api/v2/refunds",
                       reply="This still applies: owns_refund is registered only for "
                             "/api/v2/refunds; the v1 route is not covered.",
                       remaining=[{"path": "orders/app.py", "line": v2, "claim": "v2 only",
                                   "quote": 'router.use_prefix("/api/v2/refunds", owns_refund)'},
                                  {"path": "orders/app.py", "line": reg, "claim": "v1 bare",
                                   "quote": "router.register(refunds.bp)"}])

    def _thread_payments(self, step: int, messages: list) -> fg.Reply:
        if step == 0:
            return fg.call("read", paths=["orders/domain/checkout.py"], reasoning="re-walk")
        seen = _tool_text(messages)
        line = _line_of(seen, "orders/domain/checkout.py", "except PaymentDeclined as exc:")
        return fg.call("conclude_thread", claim_type="disagreement", verdict="stands",
                       reason="checkout still relies on PaymentDeclined",
                       reply="This still applies: checkout only handles declines through "
                             "`except PaymentDeclined`, which 1.5.0 no longer raises.",
                       remaining=[{"path": "orders/domain/checkout.py", "line": line,
                                   "quote": "except PaymentDeclined as exc:",
                                   "claim": "never fires with 1.5.0"}])

    def _challenger(self, step: int, task: str) -> fg.Reply:
        if step == 0:
            return fg.call("read", paths=["."], reasoning="attack the conclusion")
        if self.mode == "challenger_disagrees":
            return fg.call("challenge", outcome="disagree", reason="the cited site is not the "
                           "whole story; another path exists")
        return fg.call("challenge", outcome="agree", reason="the code supports the conclusion")

    return _thread, _thread_f1, _thread_f2, _thread_payments, _challenger


(ScriptedModel._thread, ScriptedModel._thread_f1, ScriptedModel._thread_f2,
 ScriptedModel._thread_payments, ScriptedModel._challenger) = _thread_methods()


def dump(body: dict) -> str:  # debugging aid
    return json.dumps(body, indent=1)[:4000]
