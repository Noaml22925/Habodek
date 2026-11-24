"""Line mapping through diff(finding_sha, HEAD): edits above, inside and below."""

from __future__ import annotations

import subprocess

import pytest

from habodek.brain.thread.linemap import LineMapper

BASE = "".join(f"line {i}\n" for i in range(1, 31))  # line N reads "line N"


def diff(tmp_path, new: str, *, rename: str | None = None, delete: bool = False) -> str:
    repo = tmp_path / "r"
    repo.mkdir(parents=True)

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout

    git("init", "-q")
    git("config", "user.email", "t@e")
    git("config", "user.name", "t")
    (repo / "f.py").write_text(BASE)
    git("add", "-A")
    git("commit", "-qm", "base")
    if delete:
        (repo / "f.py").unlink()
    elif rename:
        (repo / "f.py").unlink()
        (repo / rename).write_text(new)
    else:
        (repo / "f.py").write_text(new)
    git("add", "-A")
    git("commit", "-qm", "head")
    return git("diff", "-M", "HEAD~1", "HEAD")


def lines_with(edit) -> str:
    lines = BASE.splitlines(keepends=True)
    edit(lines)
    return "".join(lines)


def test_edit_above_shifts_the_anchor(tmp_path):
    new = lines_with(lambda ls: ls.insert(2, "added a\nadded b\nadded c\n"))
    mapped = LineMapper(diff(tmp_path, new)).map("f.py", 20)
    assert (mapped.line, mapped.status) == (23, "moved")


def test_edit_below_leaves_it(tmp_path):
    new = lines_with(lambda ls: ls.__setitem__(27, "changed 28\n"))
    mapped = LineMapper(diff(tmp_path, new)).map("f.py", 10)
    assert (mapped.line, mapped.status) == (10, "unchanged")


def test_edit_of_the_anchor_line_is_changed(tmp_path):
    new = lines_with(lambda ls: ls.__setitem__(19, "line 20 fixed\n"))
    mapped = LineMapper(diff(tmp_path, new)).map("f.py", 20)
    assert (mapped.line, mapped.status) == (20, "changed")
    near = LineMapper(diff(tmp_path / "x", new)).map("f.py", 19)  # context inside the hunk
    assert (near.line, near.status) == (19, "unchanged")


def test_edit_inside_with_lines_added_above_in_the_same_hunk(tmp_path):
    def edit(ls):
        ls[18] = "line 19 replaced\nextra\n"

    new = lines_with(edit)
    mapper = LineMapper(diff(tmp_path, new))
    assert mapper.map("f.py", 20).line == 21 and mapper.map("f.py", 20).status == "moved"
    assert mapper.map("f.py", 19).status == "changed"


def test_rename_and_delete(tmp_path):
    renamed = LineMapper(diff(tmp_path, BASE, rename="g.py")).map("f.py", 5)
    assert renamed.path == "g.py" and renamed.line == 5
    gone = LineMapper(diff(tmp_path / "d", "", delete=True)).map("f.py", 5)
    assert gone.status == "deleted" and gone.line is None


@pytest.mark.parametrize("line", [1, 15, 30])
def test_untouched_file(line):
    assert LineMapper.empty().map("f.py", line).status == "unchanged"
