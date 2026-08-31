"""Tests for the unified diff parser.

Anchoring correctness lives or dies here: if a line's old/new numbers are wrong,
GitLab rejects the discussion and the finding is silently lost. The fixtures below
are verbatim `git diff` output for each change type.
"""

from __future__ import annotations

from habodek.workspace.diff import is_ignored, parse_unified_diff

MODIFIED = """\
diff --git a/keep.txt b/keep.txt
index b2f931a..dd3abda 100644
--- a/keep.txt
+++ b/keep.txt
@@ -1,5 +1,6 @@
 one
-two
+TWO CHANGED
 three
 four
 five
+six added
"""

ADDED = """\
diff --git a/added.txt b/added.txt
new file mode 100644
index 0000000..ce2b18a
--- /dev/null
+++ b/added.txt
@@ -0,0 +1,2 @@
+brand new
+file here
"""

DELETED = """\
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
index 33194a0..0000000
--- a/gone.txt
+++ /dev/null
@@ -1 +0,0 @@
-old content
"""

RENAMED = """\
diff --git a/moved.txt b/renamed.txt
similarity index 100%
rename from moved.txt
rename to renamed.txt
"""

BINARY = """\
diff --git a/bin.dat b/bin.dat
new file mode 100644
index 0000000..0f49c4a
Binary files /dev/null and b/bin.dat differ
"""

MULTI_HUNK = """\
diff --git a/big.py b/big.py
index 1111111..2222222 100644
--- a/big.py
+++ b/big.py
@@ -10,3 +10,4 @@ def first():
     a = 1
     b = 2
+    c = 3
     return a
@@ -40,4 +41,4 @@ def second():
     x = 1
-    y = 2
+    y = 99
     z = 3
     return x
"""


def test_modified_file_tracks_both_sides() -> None:
    (f,) = parse_unified_diff(MODIFIED)
    assert f.status == "modified"
    assert f.path == "keep.txt"
    assert f.changed_line_count == 3  # one removed, two added

    # The edit at line 2 makes the sides diverge; everything after shifts by one.
    assert f.line_text("new", 2) == "TWO CHANGED"
    assert f.line_text("old", 2) == "two"
    assert f.line_text("new", 6) == "six added"

    anchors = f.anchorable()
    assert ("new", 2) in anchors  # the added replacement
    assert ("old", 2) in anchors  # the removed original
    assert ("new", 6) in anchors  # the appended line
    assert ("old", 6) not in anchors  # never existed on the old side


def test_added_file_anchors_new_side_only() -> None:
    (f,) = parse_unified_diff(ADDED)
    assert f.status == "added"
    assert f.path == "added.txt"
    assert f.anchorable() == {("new", 1), ("new", 2)}


def test_deleted_file_anchors_old_side_only() -> None:
    (f,) = parse_unified_diff(DELETED)
    assert f.status == "deleted"
    # A deleted file must report its old path, or the comment has nowhere to go.
    assert f.path == "gone.txt"
    assert f.anchorable() == {("old", 1)}


def test_pure_rename_has_no_hunks_but_keeps_both_paths() -> None:
    (f,) = parse_unified_diff(RENAMED)
    assert f.status == "renamed"
    assert f.old_path == "moved.txt"
    assert f.new_path == "renamed.txt"
    assert f.hunks == []
    assert f.anchorable() == set()


def test_binary_file_is_flagged() -> None:
    (f,) = parse_unified_diff(BINARY)
    assert f.is_binary is True


def test_multiple_hunks_keep_independent_line_numbering() -> None:
    (f,) = parse_unified_diff(MULTI_HUNK)
    assert len(f.hunks) == 2
    # Second hunk starts at old 40 / new 41 -- the +1 offset from the first hunk's
    # inserted line must carry across, which is where naive parsers go wrong.
    assert f.line_text("new", 12) == "    c = 3"
    assert f.line_text("new", 42) == "    y = 99"
    assert f.line_text("old", 41) == "    y = 2"


def test_all_change_types_in_one_diff() -> None:
    files = parse_unified_diff(ADDED + BINARY + DELETED + MODIFIED + RENAMED)
    assert [f.path for f in files] == [
        "added.txt",
        "bin.dat",
        "gone.txt",
        "keep.txt",
        "renamed.txt",
    ]
    assert [f.status for f in files] == [
        "added",
        "added",
        "deleted",
        "modified",
        "renamed",
    ]


def test_hunk_without_explicit_count_defaults_to_one() -> None:
    (f,) = parse_unified_diff(DELETED)
    hunk = f.hunks[0]
    assert (hunk.old_start, hunk.old_count) == (1, 1)
    assert (hunk.new_start, hunk.new_count) == (0, 0)


def test_window_gathers_surrounding_text_for_fingerprinting() -> None:
    (f,) = parse_unified_diff(MODIFIED)
    assert f.window("new", 2, radius=1) == ["one", "TWO CHANGED", "three"]


def test_unparseable_trailing_junk_is_skipped() -> None:
    files = parse_unified_diff("garbage header\n" + MODIFIED + "\\ No newline at end of file\n")
    assert len(files) == 1
    assert files[0].path == "keep.txt"


def test_is_ignored_matches_nested_and_root_paths() -> None:
    globs = ["**/*.lock", "**/node_modules/**", "**/dist/**"]
    assert is_ignored("poetry.lock", globs)  # root-level, bare
    assert is_ignored("sub/dir/poetry.lock", globs)  # nested
    assert is_ignored("node_modules/foo/index.js", globs)
    assert not is_ignored("src/app.py", globs)
    assert not is_ignored("src/locked.py", globs)
