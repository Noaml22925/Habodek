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
                                         "code": "payments-lib==1.5.0"}, chain=chain)
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


def dump(body: dict) -> str:  # debugging aid
    return json.dumps(body, indent=1)[:4000]
