"""Tier 2 for Python: jedi (pure Python, MIT), precise and offline.

jedi resolves through imports into the dependency roots and venv site-packages,
which is where internal libraries live (handoff §3.5). Its search path is set
explicitly — the reviewed tree, dependency roots, site-packages, and the standard
library — so it never resolves into the brain's own environment.
"""

from __future__ import annotations

import sysconfig
from dataclasses import dataclass
from pathlib import Path

try:
    import jedi
except ImportError:  # pragma: no cover - jedi is a declared dependency
    jedi = None


@dataclass(frozen=True)
class JediRef:
    path: Path
    line: int
    col: int  # 1-based
    is_definition: bool


class JediBackend:
    def __init__(self, project_root: Path, extra_paths: list[Path] | None = None) -> None:
        if jedi is None:
            raise RuntimeError("jedi is not installed")
        self.root = Path(project_root).resolve()
        paths = [self.root]
        if (self.root / "src").is_dir():
            paths.append(self.root / "src")
        paths += [Path(p).resolve() for p in extra_paths or []]
        stdlib = {sysconfig.get_paths()[k] for k in ("stdlib", "platstdlib")}
        self.sys_path = [str(p) for p in paths] + sorted(stdlib)
        self.project = jedi.Project(path=str(self.root), sys_path=self.sys_path,
                                    smart_sys_path=False, load_unsafe_extensions=False)

    def _script(self, path: Path):
        code = path.read_bytes().decode("utf-8", errors="replace")
        return jedi.Script(code=code, path=str(path), project=self.project)

    def references(self, path: Path, line: int, col: int) -> list[JediRef]:
        """References to the name at (line, col) — col 1-based — including its definition."""
        names = self._script(path).get_references(line, col - 1, include_builtins=False)
        out = []
        for name in names:
            if name.module_path is None or name.line is None:
                continue
            out.append(JediRef(Path(name.module_path), name.line, (name.column or 0) + 1,
                               name.is_definition()))
        return out

    def goto(self, path: Path, line: int, col: int) -> list[JediRef]:
        names = self._script(path).goto(line, col - 1, follow_imports=True,
                                        follow_builtin_imports=False)
        return [JediRef(Path(n.module_path), n.line, (n.column or 0) + 1, True)
                for n in names if n.module_path is not None and n.line is not None]
