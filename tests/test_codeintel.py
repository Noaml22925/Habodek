"""Code intelligence on small fixture repositories (M3).

The fixture: one repository with four packages (store, cache, api, jobs), a venv
site-packages holding `paylib`, and a local wheel index holding two versions of
`fakepkg`. The MR makes `get_user` return None instead of raising NotFound and bumps
`fakepkg` 1.0 -> 1.1.
"""

from __future__ import annotations

import subprocess
import textwrap
import zipfile
from pathlib import Path

import pytest

from habodek.brain.codeintel import files, pydeps
from habodek.brain.codeintel.changemap import symbol_changes
from habodek.brain.codeintel.refs import SymbolIndex
from habodek.brain.codeintel.repomap import build_repo_map, pagerank
from habodek.brain.codeintel.workspace import PathError, Workspace


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"))


BASE_FILES = {
    "store/__init__.py": "",
    "store/errors.py": """
        class NotFound(Exception):
            pass
        """,
    "store/users.py": """
        from store.errors import NotFound

        DB = {}


        def get_user(uid):
            row = DB.get(uid)
            if row is None:
                raise NotFound(uid)
            return row
        """,
    "cache/__init__.py": "",
    "cache/users.py": """
        from store.errors import NotFound
        from store.users import get_user


        class CachedUsers:
            def __init__(self):
                self.misses = set()

            def get(self, uid):
                try:
                    return get_user(uid)
                except NotFound:
                    self.misses.add(uid)
                    raise
        """,
    "api/__init__.py": "",
    "api/profile.py": """
        from paylib.client import charge
        from store.users import get_user


        def profile(uid):
            user = get_user(uid)
            return user.email


        def pay(uid):
            return charge(uid, 10)
        """,
    "jobs/__init__.py": "",
    "jobs/digest.py": """
        from store import users


        def run(ids):
            for uid in ids:
                print(users.get_user(uid).email)
        """,
    "requirements.txt": "fakepkg==1.0\nrequests==2.31.0  # public, not internal\n",
}


def make_wheel(dest: Path, version: str, body: str) -> None:
    name = f"fakepkg-{version}"
    with zipfile.ZipFile(dest / f"{name}-py3-none-any.whl", "w") as zf:
        zf.writestr("fakepkg/__init__.py", "")
        zf.writestr("fakepkg/client.py", body)
        zf.writestr(f"{name}.dist-info/METADATA",
                    f"Metadata-Version: 2.1\nName: fakepkg\nVersion: {version}\n")
        zf.writestr(f"{name}.dist-info/WHEEL",
                    "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
                    "Tag: py3-none-any\n")
        zf.writestr(f"{name}.dist-info/RECORD", "")


@pytest.fixture(scope="module")
def fixture(tmp_path_factory):
    root = tmp_path_factory.mktemp("codeintel")
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    for rel, text in BASE_FILES.items():
        write(repo, rel, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Fix NotFound handling in cache")
    write(repo, "store/users.py", """
        from store.errors import NotFound

        DB = {}


        def get_user(uid):
            row = DB.get(uid)
            if row is None:
                return None
            return row
        """)
    write(repo, "requirements.txt", "fakepkg==1.1\nrequests==2.31.0  # public, not internal\n")
    git(repo, "checkout", "-qb", "feature")
    git(repo, "commit", "-qam", "get_user returns None for a missing user")

    site = root / "site"
    write(site, "paylib/__init__.py", "")
    write(site, "paylib/client.py", """
        def charge(uid, amount):
            return {"uid": uid, "amount": amount}
        """)
    write(site, "paylib-2.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: paylib\n"
          "Version: 2.0\n")
    write(site, "paylib-2.0.dist-info/top_level.txt", "paylib\n")

    wheels = root / "wheels"
    wheels.mkdir()
    make_wheel(wheels, "1.0", "def pay(x):\n    raise Declined(x)\n")
    make_wheel(wheels, "1.1", "def pay(x):\n    return Result('declined')\n")

    ws = Workspace.from_repo(repo, "main", "feature", root / "job")
    ws.add_root("site:0", site)
    return {"ws": ws, "repo": repo, "root": root, "site": site, "wheels": wheels}


# ----------------------------------------------------------------------- files


def test_workspace_paths_round_trip(fixture):
    ws: Workspace = fixture["ws"]
    assert ws.resolve("store/users.py") == ws.head.resolve() / "store/users.py"
    assert ws.display(ws.resolve("base:store/users.py")) == "base:store/users.py"
    assert ws.display(ws.resolve("site:0:paylib/client.py")) == "site:0:paylib/client.py"
    with pytest.raises(PathError):
        ws.resolve("../../etc/passwd")
    assert ws.changed_paths() == ["requirements.txt", "store/users.py"]


def test_read_numbers_lines_pages_and_reads_base(fixture):
    ws: Workspace = fixture["ws"]
    head = files.read_file(ws, "store/users.py")
    assert "     9\t        return None" in head.text and not head.truncated
    base = files.read_file(ws, "base:store/users.py")
    assert "raise NotFound(uid)" in base.text
    big = ws.head / "big.txt"
    big.write_text("\n".join(f"line {i}" for i in range(1, 2501)))
    first = files.read_file(ws, "big.txt")
    assert (first.start, first.end, first.total_lines, first.truncated) == (1, 2000, 2500, True)
    assert "read with start=2001" in first.render()
    rest = files.read_file(ws, "big.txt", start=2001)
    assert rest.end == 2500 and not rest.truncated
    big.unlink()
    many = files.read_many(ws, ["store/users.py", "api/profile.py", "nope.py", "store"])
    assert "== nope.py: no such file" in many and "errors.py" in many
    assert "def profile" in many


def test_grep_totals_truncation_and_refusal(fixture):
    ws: Workspace = fixture["ws"]
    result = files.grep(ws, r"get_user\(")
    assert result.total_matches == 4 and result.total_files == 4
    shown = files.grep(ws, "import", show=2)
    assert "showing 2" in shown.render()
    refused = files.grep(ws, "e", refuse_over=10)
    assert refused.refused and "path_glob" in refused.render()
    assert files.grep(ws, "(unclosed").error
    assert "site:0:paylib/client.py" in files.grep(ws, "def charge", roots=["site:0"]).render()


def test_glob(fixture):
    out = files.glob(fixture["ws"], "**/users.py")
    assert "cache/users.py" in out and "store/users.py" in out


# ----------------------------------------------------------------------- symbols


def test_definitions(fixture):
    index = SymbolIndex(fixture["ws"])
    defs = index.definitions("get_user")
    assert [d.spec for d in defs] == ["store/users.py"]
    assert defs[0].definition.signature == "def get_user(uid)"
    assert index.definitions("CachedUsers.get")[0].definition.kind == "method"
    assert index.definitions("charge")[0].spec == "site:0:paylib/client.py"


def test_references_tier2_across_packages(fixture):
    index = SymbolIndex(fixture["ws"])
    result = index.references("get_user")
    assert result.tier == 2, result.note
    calls = {(h.spec, h.enclosing) for h in result.by_kind("call")}
    assert ("api/profile.py", "profile") in calls
    assert ("cache/users.py", "CachedUsers.get") in calls
    assert ("jobs/digest.py", "run") in calls  # through `users.get_user(...)`
    assert {"api", "cache", "jobs", "store"} <= {h.package for h in result.hits}
    text = result.render()
    assert "tier 2" in text and "[call] in profile" in text


def test_references_into_venv_dependency(fixture):
    index = SymbolIndex(fixture["ws"])
    result = index.references("charge")
    assert result.tier == 2, result.note
    assert ("api/profile.py", "pay") in {(h.spec, h.enclosing) for h in result.by_kind("call")}
    assert any(h.spec == "site:0:paylib/client.py" and h.kind == "definition"
               for h in result.hits)


def test_references_tier1_fallback(fixture):
    index = SymbolIndex(fixture["ws"], use_jedi=False)
    result = index.references("get_user")
    assert result.tier == 1
    # profile, CachedUsers.get, and run's attribute call `users.get_user(...)`
    assert len(result.by_kind("call")) == 3
    assert len(result.by_kind("import")) == 2


def test_callers_tree(fixture):
    text = SymbolIndex(fixture["ws"]).callers("get_user", depth=2)
    assert "- profile (api/profile.py:6)" in text
    assert "CachedUsers.get (cache/users.py" in text
    assert "- run (jobs/digest.py" in text


def test_symbol_changes_see_the_contract_change(fixture):
    changes = symbol_changes(fixture["ws"])
    (change,) = [c for c in changes.changes if c.qualname == "get_user"]
    assert change.change == "modified"
    assert "no longer raises NotFound" in change.notes
    assert any(n.startswith("returns None on 1 more path") for n in change.notes)
    assert changes.unparsed == ["requirements.txt"]
    assert change in changes.contract_changes


def test_repo_map_is_personalised(fixture):
    ws: Workspace = fixture["ws"]
    text = build_repo_map(ws, ["store/users.py"], ["get_user"], max_chars=2000)
    assert text.splitlines()[0] == "store/users.py:"
    assert "def get_user(uid)" in text


def test_pagerank_prefers_personalised_node():
    nodes = ["a", "b", "c"]
    edges = {("a", "b"): 1.0, ("b", "c"): 1.0, ("c", "a"): 1.0}
    rank = pagerank(nodes, edges, {"a": 100, "b": 1, "c": 1})
    assert rank["a"] > rank["c"] and abs(sum(rank.values()) - 1) < 1e-6


def test_git_history(fixture):
    ws: Workspace = fixture["ws"]
    log = ws.git_log("store/users.py")
    assert "get_user returns None" in log and "Fix NotFound handling" in log
    blame = ws.git_blame("store/users.py", 9, 9)
    assert "return None" in blame


# ----------------------------------------------------------------------- deps


def test_pins_from_both_revisions(fixture):
    repo = fixture["repo"]
    assert pydeps.pins_at(repo, "main")["fakepkg"].version == "1.0"
    head = pydeps.pins_at(repo, "feature")
    assert head["fakepkg"].version == "1.1" and head["requests"].version == "2.31.0"


def test_pins_from_pyproject_and_locks(tmp_path):
    write(tmp_path, "pyproject.toml", """
        [project]
        dependencies = ["Platform_Common==2.3.0", "httpx>=0.27"]
        [tool.poetry.dependencies]
        payments-lib = "1.4.2"
        """)
    write(tmp_path, "uv.lock", """
        [[package]]
        name = "httpx"
        version = "0.28.1"
        """)
    pins = pydeps.pins_in_dir(tmp_path)
    assert pins["platform-common"].version == "2.3.0"
    assert pins["payments-lib"].version == "1.4.2"
    assert pins["httpx"].version == "0.28.1" and pins["httpx"].source == "uv.lock"


def test_version_bump_library_diff(fixture, tmp_path):
    resolver = pydeps.SourceResolver(tmp_path / "cache", find_links=[fixture["wheels"]])
    deps = pydeps.resolve_dependencies(pydeps.pins_at(fixture["repo"], "main"),
                                       pydeps.pins_at(fixture["repo"], "feature"),
                                       ["fakepkg"], resolver)
    (dep,) = deps
    assert dep.bumped and (dep.base_version, dep.head_version) == ("1.0", "1.1")
    assert (dep.head_path / "fakepkg/client.py").read_text().startswith("def pay")
    diff = pydeps.library_diff(dep.base_path, dep.head_path, "fakepkg@1.0", "fakepkg@1.1")
    assert "-    raise Declined(x)" in diff and "+    return Result('declined')" in diff
    assert set(dep.labels()) == {"dep:fakepkg@1.1", "dep:fakepkg@1.0"}
    # Cached the second time: no pip call.
    again = pydeps.SourceResolver(tmp_path / "cache")
    assert again.source("fakepkg", "1.1") == dep.head_path


def test_source_from_site_packages(fixture, tmp_path):
    resolver = pydeps.SourceResolver(tmp_path / "cache", site_dirs=[fixture["site"]])
    path = resolver.source("paylib", "2.0")
    assert (path / "paylib/client.py").is_file()
    assert resolver.source("paylib", "9.9") is None
    assert "no index configured" in resolver.log[-1]


def test_outline(fixture):
    text = SymbolIndex(fixture["ws"]).outline("cache/users.py")
    assert "5-14  class CachedUsers" in text and "9-14    def get(self, uid)" in text
    assert "no symbol backend" in SymbolIndex(fixture["ws"]).outline("requirements.txt")


def test_tier2_keeps_unresolved_lexical_matches(tmp_path):
    write(tmp_path, "svc.py", """
        class Service:
            def check(self):
                return 1
        """)
    write(tmp_path, "use.py", """
        def run(svc):
            return svc.check()
        """)
    result = SymbolIndex(Workspace(head=tmp_path)).references("Service.check")
    assert result.tier == 2
    (hit,) = [h for h in result.hits if h.spec == "use.py"]
    assert hit.kind == "call" and not hit.confirmed
    assert "[call?] in run" in result.render() and "could not resolve" in result.note
