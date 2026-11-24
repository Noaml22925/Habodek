"""Build the scenario fixtures: three git repositories, a wheel index, resolved expectations.

    python -m eval.scenarios.build [--out DIR]      (default eval/scenarios/_build)

Deterministic: fixed authors and commit dates, so the same sources give the same SHAs.
The output is ignored by git; it is rebuilt by tests and by `habodek-brain scorecard`.

Layout of DIR:
  repos/platform-common, repos/payments-lib, repos/orders-service   (git, with branches)
  index/        wheels: platform-common 2.3.0, payments-lib 1.4.2 and 1.5.0
  expected/<n>.json   the scenario's expectations with line numbers resolved
  findings/<n>.json   for thread scenarios: the stored finding (F1/F2), lines resolved
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import textwrap
import zipfile
from pathlib import Path

from eval.scenarios import specs
from eval.scenarios.repos import orders_service as osvc
from eval.scenarios.repos import payments_lib as pay
from eval.scenarios.repos import platform_common as pc

DEFAULT_OUT = Path(__file__).parent / "_build"
_EPOCH = 1_767_225_600  # 2026-01-01T00:00:00Z
VERSION = "4"  # bump when the sources change, so stale builds are rebuilt


class _Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.clock = 0
        path.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Scenario Bot")
        self.git("config", "user.email", "scenarios@example.invalid")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str) -> str:
        self.clock += 3600
        stamp = f"{_EPOCH + self.clock} +0000"
        env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp,
               "GIT_AUTHOR_NAME": "Scenario Bot", "GIT_COMMITTER_NAME": "Scenario Bot",
               "GIT_AUTHOR_EMAIL": "scenarios@example.invalid",
               "GIT_COMMITTER_EMAIL": "scenarios@example.invalid"}
        return subprocess.run(["git", "-C", str(self.path), *args], check=True, env=env,
                              capture_output=True, text=True).stdout

    def write(self, files: dict[str, str]) -> None:
        for rel, text in files.items():
            target = self.path / rel
            if text is None:
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            body = textwrap.dedent(text).lstrip("\n")
            target.write_text(body if body.endswith("\n") or not body else body + "\n")

    def commit(self, message: str, files: dict[str, str]) -> None:
        self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def branch(self, name: str, start: str) -> None:
        self.git("checkout", "-q", "-B", name, start)


def _changed(original: str, new: str, what: str) -> str:
    if original == new:
        raise AssertionError(f"scenario source edit did not apply: {what}")
    return new


# --------------------------------------------------------------------------- repos


def build_platform_common(root: Path) -> None:
    repo = _Repo(root / "platform-common")
    initial = {k: v for k, v in pc.FILES.items() if k not in (
        "platform_common/web/admin.py", "platform_common/store/orgs.py",
        "tests/test_router.py")}
    poisoned = _changed(pc.FILES["platform_common/cache/users.py"],
                        pc.FILES["platform_common/cache/users.py"].replace(
                            "        except NotFound:\n", "        except Exception:\n"),
                        "cache poisoning")
    initial["platform_common/cache/users.py"] = poisoned
    repo.commit("Initial import of platform-common", initial)
    repo.commit("Fix negative cache poisoning on NotFound",
                {"platform_common/cache/users.py": pc.FILES["platform_common/cache/users.py"]})
    repo.commit("Add AdminBlueprint and the org store", {
        "platform_common/web/admin.py": pc.FILES["platform_common/web/admin.py"],
        "platform_common/store/orgs.py": pc.FILES["platform_common/store/orgs.py"],
        "tests/test_router.py": pc.FILES["tests/test_router.py"],
    })
    repo.git("tag", "v2.3.0")
    repo.branch("mr/s1-get-user-none", "v2.3.0")
    _changed(pc.FILES["platform_common/store/users.py"], pc.S1_USERS, "S1 users")
    _changed(pc.FILES["tests/test_users.py"], pc.S1_TESTS, "S1 tests")
    _changed(pc.FILES["tests/test_cache.py"], pc.S1_CACHE_TESTS, "S1 cache tests")
    repo.commit("Return None from get_user for missing users",
                {"platform_common/store/users.py": pc.S1_USERS,
                 "tests/test_users.py": pc.S1_TESTS,
                 "tests/test_cache.py": pc.S1_CACHE_TESTS})
    repo.branch("mr/s5-restore-raise", "mr/s1-get-user-none")
    repo.commit("Raise NotFound again from get_user",
                {"platform_common/store/users.py": pc.FILES["platform_common/store/users.py"],
                 "tests/test_users.py": pc.FILES["tests/test_users.py"],
                 "tests/test_cache.py": pc.FILES["tests/test_cache.py"]})
    repo.branch("mr/s7-unrelated", "mr/s1-get-user-none")
    _changed(pc.FILES["platform_common/store/orgs.py"], pc.S7_ORGS, "S7 orgs")
    repo.commit("Document get_org", {"platform_common/store/orgs.py": pc.S7_ORGS})
    repo.git("checkout", "-q", "main")


def build_payments_lib(root: Path) -> None:
    repo = _Repo(root / "payments-lib")
    repo.commit("Initial import of payments-lib", pay.FILES_1_4_2)
    repo.git("tag", "1.4.2")
    repo.commit("Return declined charges as results instead of raising", pay.FILES_1_5_0)
    repo.git("tag", "1.5.0")


def build_orders_service(root: Path) -> None:
    repo = _Repo(root / "orders-service")
    initial = {k: v for k, v in osvc.FILES.items() if k not in (
        "orders/api/v2.py", "orders/middleware/__init__.py", "orders/middleware/ownership.py",
        "orders/storage/refunds_store.py")}
    initial["orders/domain/checkout.py"] = _changed(osvc.FILES["orders/domain/checkout.py"],
                                                    osvc.INITIAL_CHECKOUT, "initial checkout")
    initial["orders/app.py"] = osvc.INITIAL_APP
    initial["orders/jobs/digest.py"] = osvc.INITIAL_DIGEST
    repo.commit("Initial import of orders-service", initial)
    repo.commit("Fix double charge on retry",
                {"orders/domain/checkout.py": osvc.FILES["orders/domain/checkout.py"]})
    repo.commit("Add refunds API v2 with ownership middleware", {
        k: osvc.FILES[k] for k in ("orders/api/v2.py", "orders/middleware/__init__.py",
                                   "orders/middleware/ownership.py",
                                   "orders/storage/refunds_store.py", "orders/app.py")})
    repo.commit("Digest skips deleted subscribers",
                {"orders/jobs/digest.py": osvc.FILES["orders/jobs/digest.py"]})

    repo.branch("mr/s2-payments-1.5.0", "main")
    repo.commit("Bump payments-lib to 1.5.0", {"requirements.txt": osvc.S2_REQUIREMENTS})

    repo.branch("mr/s3-validate-quantity", "main")
    for name, new in (("orders/api/orders.py", osvc.S3_ORDERS),
                      ("orders/jobs/import_orders.py", osvc.S3_IMPORT),
                      ("orders/domain/cart.py", osvc.S3_CART),
                      ("orders/validation.py", osvc.S3_VALIDATION)):
        _changed(osvc.FILES[name], new, f"S3 {name}")
    repo.commit("Move quantity validation into validation.validate_quantity", {
        "orders/api/orders.py": osvc.S3_ORDERS, "orders/jobs/import_orders.py": osvc.S3_IMPORT,
        "orders/domain/cart.py": osvc.S3_CART, "orders/validation.py": osvc.S3_VALIDATION})

    repo.branch("mr/s4-admin-refunds", "main")
    repo.commit("Admin: list all refunds", {"orders/api/admin.py": osvc.S4_ADMIN})

    for branch, app in (("mr/s8-refunds-v1", osvc.APP_REFUNDS_V1_COVERED),
                        ("mr/s9-refunds-v1", osvc.APP_REFUNDS_V1_UNCOVERED)):
        _changed(osvc.FILES["orders/app.py"], app, branch)
        repo.branch(branch, "main")
        repo.commit("Refunds API v1", {"orders/api/refunds.py": osvc.REFUNDS_V1,
                                       "orders/app.py": app})

    repo.branch("s6/profile-none-check", "main")
    _changed(osvc.FILES["orders/api/profile.py"], osvc.S6_PROFILE, "S6 profile")
    repo.commit("Handle a missing user in the profile endpoint",
                {"orders/api/profile.py": osvc.S6_PROFILE})
    repo.git("checkout", "-q", "main")


# --------------------------------------------------------------------------- wheels


def _wheel(index: Path, dist: str, version: str, files: dict[str, str], package: str) -> None:
    norm = dist.replace("-", "_")
    name = f"{norm}-{version}"
    info = f"{name}.dist-info"
    entries: list[tuple[str, bytes]] = []
    for rel, text in sorted(files.items()):
        if rel.startswith(package + "/"):
            body = textwrap.dedent(text).lstrip("\n")
            entries.append((rel, (body if body.endswith("\n") or not body else body + "\n")
                            .encode()))
    entries.append((f"{info}/METADATA",
                    f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n".encode()))
    entries.append((f"{info}/WHEEL", b"Wheel-Version: 1.0\nGenerator: habodek-scenarios\n"
                    b"Root-Is-Purelib: true\nTag: py3-none-any\n"))
    entries.append((f"{info}/top_level.txt", f"{package}\n".encode()))
    record = "".join(f"{rel},sha256={hashlib.sha256(data).hexdigest()},{len(data)}\n"
                     for rel, data in entries) + f"{info}/RECORD,,\n"
    entries.append((f"{info}/RECORD", record.encode()))
    with zipfile.ZipFile(index / f"{name}-py3-none-any.whl", "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, data in entries:
            item = zipfile.ZipInfo(rel, date_time=(2026, 1, 1, 0, 0, 0))
            item.external_attr = 0o644 << 16
            zf.writestr(item, data)


# --------------------------------------------------------------------------- expectations


def _show(repo: Path, ref: str, path: str) -> list[str]:
    return subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{path}"], check=True,
                          capture_output=True, text=True).stdout.splitlines()


def _sha(repo: Path, ref: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], check=True,
                          capture_output=True, text=True).stdout.strip()


def resolve_line(repo: Path, ref: str, path: str, text: str) -> int:
    lines = _show(repo, ref, path)
    hits = [i for i, line in enumerate(lines, 1) if text in line]
    if len(hits) != 1:
        raise AssertionError(f"{path}@{ref}: {text!r} found {len(hits)} times, need 1")
    return hits[0]


def _resolve_finding(root: Path, finding: dict, made_at: str) -> dict:
    repos = root / "repos"
    own = repos / finding["repo"]
    out = {k: v for k, v in finding.items() if k not in ("anchor", "chain", "repo", "made_at")}
    anchor = finding["anchor"]
    out["anchor"] = {"path": anchor["path"], "side": "new", "snippet": anchor["text"],
                     "line": resolve_line(own, made_at, anchor["path"], anchor["text"])}
    chain = []
    for link in finding["chain"]:
        repo_name = link.get("repo")
        repo = repos / repo_name if repo_name else own
        ref = "main" if repo_name else made_at
        line = resolve_line(repo, ref, link["path"], link["text"])
        quote = _show(repo, ref, link["path"])[line - 1].strip()
        chain.append({"path": link["path"], "line": line, "quote": quote,
                      "claim": link["claim"], "repo": repo_name})
    out["chain"] = chain
    out["head_sha"] = _sha(own, made_at)
    out["status"] = "posted"
    return out


def write_expectations(root: Path) -> None:
    repos = root / "repos"
    (root / "expected").mkdir()
    (root / "findings").mkdir()
    for sid, scenario in specs.SCENARIOS.items():
        expect = json.loads(json.dumps(scenario["expect"]))
        repo = repos / scenario["repo"]
        if scenario["kind"] == "review" and "anchor" in expect:
            anchor = expect["anchor"]
            anchor["line"] = resolve_line(repo, scenario["head"], anchor["path"], anchor["text"])
        (root / "expected" / f"{sid}.json").write_text(json.dumps(expect, indent=2))
        if scenario["kind"] == "thread":
            fid = scenario["finding"]
            made_at = specs.F2_MADE_AT.get(sid) or specs.FINDINGS[fid]["made_at"]
            resolved = _resolve_finding(root, specs.FINDINGS[fid], made_at)
            (root / "findings" / f"{sid}.json").write_text(json.dumps(resolved, indent=2))


# --------------------------------------------------------------------------- main


def build(out: Path | str = DEFAULT_OUT, force: bool = False) -> Path:
    out = Path(out).resolve()
    stamp = out / "BUILD"
    if stamp.is_file() and stamp.read_text().strip() == VERSION and not force:
        return out
    if out.exists():
        shutil.rmtree(out)
    (out / "repos").mkdir(parents=True)
    build_platform_common(out / "repos")
    build_payments_lib(out / "repos")
    build_orders_service(out / "repos")
    index = out / "index"
    index.mkdir()
    _wheel(index, "platform-common", "2.3.0", pc.FILES, "platform_common")
    _wheel(index, "payments-lib", "1.4.2", pay.FILES_1_4_2, "payments")
    _wheel(index, "payments-lib", "1.5.0", pay.FILES_1_5_0, "payments")
    write_expectations(out)
    stamp.write_text(VERSION + "\n")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    print(build(args.out, force=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
