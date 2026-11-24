"""Map a line from the revision a finding was made at to HEAD, through the diff.

`diff(finding_sha, HEAD)` says, per file, which lines were removed and added. A line
of the old file is then:
- `unchanged` (possibly moved): it survives as a context line; we return its new number;
- `changed`: it was removed or replaced; we return where its hunk now starts, so the
  agent reads the new code there;
- `deleted`: the whole file was deleted.
Renames are followed (`git diff -M`).
"""

from __future__ import annotations

from dataclasses import dataclass

from habodek.workspace.diff import FileChange, parse_unified_diff


@dataclass(frozen=True)
class Mapped:
    path: str  # the path at HEAD
    line: int | None
    status: str  # unchanged | moved | changed | deleted


class LineMapper:
    def __init__(self, diff_text: str) -> None:
        self.files: dict[str, FileChange] = {}
        for change in parse_unified_diff(diff_text):
            self.files[change.old_path] = change

    @classmethod
    def empty(cls) -> LineMapper:
        return cls("")

    def changed_paths(self) -> set[str]:
        return {f.new_path for f in self.files.values()} | set(self.files)

    def map(self, path: str, line: int) -> Mapped:
        change = self.files.get(path)
        if change is None:
            return Mapped(path, line, "unchanged")
        if change.status == "deleted":
            return Mapped(path, None, "deleted")
        new_path = change.new_path
        offset = 0  # new - old for lines after the hunks seen so far
        for hunk in change.hunks:
            old_end = hunk.old_start + max(hunk.old_count, 1) - 1
            if line < hunk.old_start:
                break
            if line > old_end and hunk.old_count > 0:
                offset = (hunk.new_start + hunk.new_count) - (hunk.old_start + hunk.old_count)
                continue
            # Inside the hunk: find the line itself.
            for diff_line in hunk.lines:
                if diff_line.old_lineno == line:
                    if diff_line.kind == "context":
                        status = "moved" if diff_line.new_lineno != line else "unchanged"
                        return Mapped(new_path, diff_line.new_lineno, status)
                    return Mapped(new_path, self._nearest_new(hunk, line), "changed")
            offset = (hunk.new_start + hunk.new_count) - (hunk.old_start + hunk.old_count)
        new_line = line + offset
        status = "unchanged" if new_line == line and new_path == path else "moved"
        return Mapped(new_path, new_line, status)

    @staticmethod
    def _nearest_new(hunk, old_line: int) -> int:
        """Where the replaced code now is: the first added line after the removal point,
        else the next context line, else the hunk start."""
        seen = False
        for diff_line in hunk.lines:
            if diff_line.old_lineno == old_line:
                seen = True
                continue
            if seen and diff_line.new_lineno is not None:
                return diff_line.new_lineno
        return hunk.new_start
