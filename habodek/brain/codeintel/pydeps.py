"""Source of internal Python dependencies, at the version a commit pins (handoff §3.5).

1. Pins come from the repository at that commit: requirements files, `pyproject.toml`
   (PEP 621 and Poetry), and `uv.lock` / `poetry.lock` / `pdm.lock` (a lock wins).
2. For each internal package (a configured list of names or fnmatch patterns) the
   source at that version comes from a configured venv's site-packages if that exact
   version is installed there, else from `pip download --no-deps` against the
   configured index (Artifactory) into `deps-cache/<pkg>/<version>/`, unpacked.
3. A version bump is a change to review: when base and head pin different versions,
   both are unpacked and the library diff between them is part of the evidence.

Reverse dependencies (an MR in a library, consumers elsewhere) come with the
cartographer in M10.
"""

from __future__ import annotations

import difflib
import fnmatch
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from habodek.workspace.gitcmd import GitError, run_git

_REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*===?\s*([^\s;#,]+)")
_SOURCE_SUFFIXES = (".py", ".pyi", ".txt", ".md", ".cfg", ".toml", ".json", ".yaml", ".yml")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Pin:
    name: str  # normalized
    version: str
    source: str  # the file the pin came from


# --------------------------------------------------------------------------- pins


def _requirements(text: str, source: str, read_more) -> dict[str, Pin]:
    pins: dict[str, Pin] = {}
    for raw in text.splitlines():
        line = raw.split(" #")[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "--requirement ")):
            included = line.split(None, 1)[1].strip()
            more = read_more(included)
            if more is not None:
                pins.update(_requirements(more, included, read_more))
            continue
        match = _REQ_RE.match(line)
        if match:
            pins[normalize(match.group(1))] = Pin(normalize(match.group(1)), match.group(3),
                                                  source)
    return pins


def _pyproject(text: str, source: str) -> dict[str, Pin]:
    data = tomllib.loads(text)
    pins: dict[str, Pin] = {}
    specs = list((data.get("project") or {}).get("dependencies") or [])
    for group in ((data.get("project") or {}).get("optional-dependencies") or {}).values():
        specs += list(group)
    for spec in specs:
        match = _REQ_RE.match(spec)
        if match:
            pins[normalize(match.group(1))] = Pin(normalize(match.group(1)), match.group(3),
                                                  source)
    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
    for name, value in poetry.items():
        version = value.get("version") if isinstance(value, dict) else value
        if isinstance(version, str):
            exact = re.fullmatch(r"=?=?\s*(\d[^\s,*^~<>]*)", version.strip())
            if exact:
                pins[normalize(name)] = Pin(normalize(name), exact.group(1), source)
    return pins


def _lock(text: str, source: str) -> dict[str, Pin]:
    data = tomllib.loads(text)
    pins: dict[str, Pin] = {}
    for package in data.get("package") or []:
        name, version = package.get("name"), package.get("version")
        if name and version:
            pins[normalize(name)] = Pin(normalize(name), str(version), source)
    return pins


def read_pins(read) -> dict[str, Pin]:
    """Pins from a tree; `read(relpath) -> str | None` abstracts worktree vs git."""
    pins: dict[str, Pin] = {}
    for name in ("requirements.txt", "requirements.in", "requirements/base.txt",
                 "requirements/prod.txt", "requirements/main.txt"):
        text = read(name)
        if text is not None:
            base_dir = name.rsplit("/", 1)[0] + "/" if "/" in name else ""
            pins.update(_requirements(text, name, lambda rel, d=base_dir: read(d + rel)))
    text = read("pyproject.toml")
    if text is not None:
        try:
            pins.update(_pyproject(text, "pyproject.toml"))
        except tomllib.TOMLDecodeError:
            pass
    for lock in ("poetry.lock", "pdm.lock", "uv.lock"):
        text = read(lock)
        if text is not None:
            try:
                pins.update(_lock(text, lock))
            except tomllib.TOMLDecodeError:
                pass
    return pins


def pins_in_dir(root: Path) -> dict[str, Pin]:
    def read(rel: str) -> str | None:
        path = root / rel
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None

    return read_pins(read)


def pins_at(repo: Path, rev: str) -> dict[str, Pin]:
    def read(rel: str) -> str | None:
        try:
            return run_git(repo, "show", f"{rev}:{rel}")
        except GitError:
            return None

    return read_pins(read)


def is_internal(name: str, patterns: tuple[str, ...] | list[str]) -> bool:
    name = normalize(name)
    return any(fnmatch.fnmatch(name, normalize(p)) or fnmatch.fnmatch(name, p.lower())
               for p in patterns)


# --------------------------------------------------------------------------- sources


@dataclass
class SourceResolver:
    cache_dir: Path
    site_dirs: list[Path] = field(default_factory=list)
    index_url: str = ""  # Artifactory simple index (HABODEK_PIP_INDEX_URL)
    find_links: list[Path] = field(default_factory=list)  # local wheel dirs (tests, offline)
    pip_timeout: float = 300.0
    log: list[str] = field(default_factory=list)

    def source(self, name: str, version: str) -> Path | None:
        """Unpacked source of `name==version`, or None (the reason is in `log`)."""
        name = normalize(name)
        target = self.cache_dir / name / version
        if target.is_dir() and any(target.iterdir()):
            return target
        found = self._from_site(name, version, target)
        if found is not None:
            self.log.append(f"{name}=={version}: from site-packages")
            return found
        return self._download(name, version, target)

    def _from_site(self, name: str, version: str, target: Path) -> Path | None:
        for site in self.site_dirs:
            for info in Path(site).glob("*.dist-info"):
                meta = info / "METADATA"
                if not meta.is_file():
                    continue
                fields = _metadata(meta.read_text(encoding="utf-8", errors="replace"))
                if normalize(fields.get("name", "")) != name or fields.get("version") != version:
                    continue
                tops = _top_levels(info)
                if not tops:
                    continue
                target.mkdir(parents=True, exist_ok=True)
                for top in tops:
                    src = Path(site) / top
                    if src.is_dir():
                        shutil.copytree(src, target / top, dirs_exist_ok=True,
                                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                    elif src.with_suffix(".py").is_file():
                        shutil.copy2(src.with_suffix(".py"), target / f"{top}.py")
                return target
        return None

    def _download(self, name: str, version: str, target: Path) -> Path | None:
        if not self.index_url and not self.find_links:
            self.log.append(f"{name}=={version}: not installed and no index configured")
            return None
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [sys.executable, "-m", "pip", "download", "--no-deps",
                   "--disable-pip-version-check", "--quiet", "--dest", tmp,
                   f"{name}=={version}"]
            if self.find_links:
                cmd += ["--no-index"]
                for link in self.find_links:
                    cmd += ["--find-links", str(link)]
            else:
                cmd += ["--index-url", self.index_url]
            proc = subprocess.run(cmd, capture_output=True, timeout=self.pip_timeout)
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
                self.log.append(f"{name}=={version}: pip download failed: "
                                f"{err[-1] if err else proc.returncode}")
                return None
            archives = sorted(Path(tmp).iterdir())
            if not archives:
                self.log.append(f"{name}=={version}: pip download produced nothing")
                return None
            target.mkdir(parents=True, exist_ok=True)
            _unpack(archives[0], target)
        self.log.append(f"{name}=={version}: downloaded {archives[0].name}")
        return target


def _metadata(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            break
        key, _, value = line.partition(":")
        out.setdefault(key.strip().lower(), value.strip())
    return out


def _top_levels(info: Path) -> list[str]:
    top_level = info / "top_level.txt"
    if top_level.is_file():
        return [t.strip() for t in top_level.read_text().splitlines() if t.strip()]
    record = info / "RECORD"
    tops: list[str] = []
    if record.is_file():
        for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
            first = line.split(",")[0].split("/")[0]
            if first and not first.endswith((".dist-info", ".data")) and first != "..":
                top = first[:-3] if first.endswith(".py") else first
                if top not in tops and top != "__pycache__":
                    tops.append(top)
    return tops


def _unpack(archive: Path, target: Path) -> None:
    name = archive.name
    if name.endswith(".whl") or name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                if ".dist-info/" in member or member.endswith("/"):
                    continue
                _safe_extract(zf.read(member), target, member)
        return
    if name.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                parts = member.name.split("/")
                rel = "/".join(parts[1:]) if len(parts) > 1 else parts[0]  # strip pkg-1.0/
                handle = tf.extractfile(member)
                if handle is not None and rel:
                    _safe_extract(handle.read(), target, rel)
        return
    raise ValueError(f"unknown archive type: {name}")


def _safe_extract(data: bytes, target: Path, member: str) -> None:
    dest = (target / member).resolve()
    if target.resolve() not in dest.parents:
        return  # path traversal in an archive: skip it
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


# --------------------------------------------------------------------------- bumps


@dataclass
class DependencySource:
    name: str
    base_version: str | None
    head_version: str | None
    base_path: Path | None
    head_path: Path | None

    @property
    def bumped(self) -> bool:
        return self.base_version != self.head_version

    def labels(self) -> dict[str, Path]:
        out: dict[str, Path] = {}
        if self.head_path is not None and self.head_version:
            out[f"dep:{self.name}@{self.head_version}"] = self.head_path
        if self.base_path is not None and self.base_version and self.bumped:
            out[f"dep:{self.name}@{self.base_version}"] = self.base_path
        return out


def resolve_dependencies(base_pins: dict[str, Pin], head_pins: dict[str, Pin],
                         patterns: tuple[str, ...] | list[str],
                         resolver: SourceResolver) -> list[DependencySource]:
    names = sorted({n for n in (*base_pins, *head_pins) if is_internal(n, patterns)})
    out: list[DependencySource] = []
    for name in names:
        base_v = base_pins[name].version if name in base_pins else None
        head_v = head_pins[name].version if name in head_pins else None
        head_path = resolver.source(name, head_v) if head_v else None
        base_path = resolver.source(name, base_v) if base_v and base_v != head_v else head_path
        out.append(DependencySource(name, base_v, head_v, base_path, head_path))
    return out


def library_diff(old_root: Path, new_root: Path, old_label: str, new_label: str,
                 max_chars: int = 60_000) -> str:
    """Unified diff of two unpacked versions of a library (source files only)."""
    def files(root: Path) -> dict[str, Path]:
        return {p.relative_to(root).as_posix(): p for p in root.rglob("*")
                if p.is_file() and p.suffix in _SOURCE_SUFFIXES and "__pycache__" not in p.parts}

    old, new = files(old_root), files(new_root)
    chunks: list[str] = []
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    if added:
        chunks.append("added files: " + ", ".join(added))
    if removed:
        chunks.append("removed files: " + ", ".join(removed))
    for rel in sorted(set(old) | set(new)):
        a = old[rel].read_text("utf-8", "replace").splitlines(keepends=True) if rel in old else []
        b = new[rel].read_text("utf-8", "replace").splitlines(keepends=True) if rel in new else []
        if a == b:
            continue
        chunks.append("".join(difflib.unified_diff(a, b, f"{old_label}/{rel}",
                                                   f"{new_label}/{rel}", n=3)))
    text = "\n".join(chunks)
    if len(text) > max_chars:
        text = text[:max_chars] + (f"\n... library diff truncated at {max_chars} characters; "
                                   "read the files under both dep: roots for the rest")
    return text or "(no source differences)"
