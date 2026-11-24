"""Env-prep: build a repository's virtualenv for the sandbox, from its declared dependencies.

**Standard library only, Python 3.8+.** This file runs inside the runner image (or on
the host, for the local backend) where Habodek is not installed. The Go control plane
embeds a byte-identical copy (`agent/internal/pkg/sandbox/envprep.py`; a test on each
side checks they match).

    python envprep.py --src DIR --venv DIR [--recipe FILE] [--index-url URL]
                      [--find-links DIR ...]
    python envprep.py --key --src DIR [--recipe FILE] [--tag TAG]

What it installs, in this order (handoff §3.6, M9):

1. the repository's requirement files (`requirements.txt`, `requirements-dev.txt`, ...),
   with index and local-project lines removed;
2. `[project].dependencies` from `pyproject.toml`, and the optional-dependency and
   dependency groups named test/tests/testing/dev;
3. the recipe's `install:` and `tools:` lines (`.habodek.md`, see `parse_recipe`).

The project itself is not installed: a run puts its copy of the worktree on
`PYTHONPATH`, so the code under test is always the code of the revision being run.
The venv sees the image's site-packages (`--system-site-packages`), which is where the
runner image keeps its test tools (pytest). Where packages come from is fixed by the
control plane (`--index-url`, Artifactory on the VPN), never by the repository:
index options in requirement files and recipes are dropped.

The venv is cached by `cache_key`: a hash of the dependency files, the recipe and the
interpreter. `habodek-env.json` in the venv marks it complete.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

SCRIPT_VERSION = "1"

REQUIREMENT_FILES = (
    "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
    "requirements_dev.txt", "requirements_test.txt", "dev-requirements.txt",
    "test-requirements.txt",
)
# Files whose content decides what env-prep installs (the cache key).
KEY_FILES = REQUIREMENT_FILES + (
    "pyproject.toml", "setup.cfg", "setup.py", "uv.lock", "poetry.lock", "pdm.lock",
)
GROUPS = ("test", "tests", "testing", "dev")
RECIPE_FENCE = "habodek-sandbox"
RECIPE_KEYS = ("test", "install", "tools", "timeout")
MARKER = "habodek-env.json"

# pip options a repository may not set: they choose where packages come from.
_BANNED_OPTIONS = ("-i", "--index-url", "--extra-index-url", "-f", "--find-links",
                   "--trusted-host", "--no-index", "--pre", "--prefix", "--target",
                   "--root", "--src", "-e", "--editable")
_LOCAL_PROJECT = re.compile(r"^(\.|\./?|file:.*|\.\./.*|/.*)(\[[^\]]*\])?$")


# --------------------------------------------------------------------------- recipe


def parse_recipe(text):
    """The `habodek-sandbox` block of a `.habodek.md`, as {key: [values]}.

    ```habodek-sandbox
    test: python -m pytest -q
    install: -r requirements/ci.txt
    tools: pytest-asyncio==0.23.8
    timeout: 300
    ```

    Unknown keys are ignored; a key may repeat. Returns {} when there is no block.
    """
    out = {}
    inside = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("```"):
            if inside:
                break
            inside = line[3:].strip().lower() == RECIPE_FENCE
            continue
        if not inside or not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip().lower(), value.strip()
        if key in RECIPE_KEYS and value:
            out.setdefault(key, []).append(value)
    return out


def read_recipe(path):
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        return parse_recipe(fh.read())


# --------------------------------------------------------------------------- key


def cache_key(src, recipe_text="", tag=""):
    """sha256 over the dependency files (name + content), the recipe and the interpreter.

    The Go control plane computes the same key (sandbox/key.go); a shared fixture pins
    both to one value.
    """
    h = hashlib.sha256()
    h.update(("habodek-envprep/" + SCRIPT_VERSION + "\n").encode())
    h.update(("tag:" + tag + "\n").encode())
    names = [n for n in KEY_FILES if os.path.isfile(os.path.join(src, n))]
    req_dir = os.path.join(src, "requirements")
    if os.path.isdir(req_dir):
        names += sorted("requirements/" + n for n in os.listdir(req_dir) if n.endswith(".txt"))
    for name in sorted(names):
        with open(os.path.join(src, name), "rb") as fh:
            data = fh.read()
        h.update(("file:" + name + ":" + str(len(data)) + "\n").encode())
        h.update(data)
    recipe = (recipe_text or "").encode()
    h.update(("recipe:" + str(len(recipe)) + "\n").encode())
    h.update(recipe)
    return h.hexdigest()[:32]


# --------------------------------------------------------------------------- what to install


def _inside(src, path):
    real_src = os.path.realpath(src)
    real = os.path.realpath(path)
    return real == real_src or real.startswith(real_src + os.sep)


def sanitize_requirements(src, rel, out_dir, seen=None, log=None):
    """Copy a requirement file into out_dir without index options or local projects.

    Nested `-r`/`-c` references are followed (inside the repository only). Returns the
    path of the sanitised copy, or None if the file is outside the repository.
    """
    seen = set() if seen is None else seen
    log = [] if log is None else log
    path = os.path.normpath(os.path.join(src, rel))
    if not _inside(src, path) or not os.path.isfile(path) or path in seen:
        return None
    seen.add(path)
    kept = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.split(" #", 1)[0].strip()
            if not line or line.startswith("#"):
                continue
            head = line.split()[0]
            option = head.split("=", 1)[0]
            if option in ("-r", "--requirement", "-c", "--constraint"):
                target = line[len(head):].strip() if "=" not in head else head.split("=", 1)[1]
                nested = sanitize_requirements(
                    src, os.path.join(os.path.dirname(os.path.relpath(path, src)), target),
                    out_dir, seen, log)
                if nested:
                    kept.append(("-c " if option in ("-c", "--constraint") else "-r ") + nested)
                else:
                    log.append("dropped %s: %s (outside the repository or missing)" % (rel, line))
                continue
            if option in _BANNED_OPTIONS or option.startswith("--"):
                log.append("dropped %s: %s (pip options are set by the control plane)"
                           % (rel, line))
                continue
            if _LOCAL_PROJECT.match(head):
                log.append("dropped %s: %s (the project is put on PYTHONPATH instead)"
                           % (rel, line))
                continue
            kept.append(line)
    name = "req-%d-%s" % (len(seen), os.path.basename(path))
    copy = os.path.join(out_dir, name)
    with open(copy, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + "\n")
    return copy


def _toml(path):
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover - the runner image is 3.12
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, ValueError):
        return {}


def pyproject_requirements(src):
    data = _toml(os.path.join(src, "pyproject.toml"))
    project = data.get("project") or {}
    reqs = list(project.get("dependencies") or [])
    optional = project.get("optional-dependencies") or {}
    groups = data.get("dependency-groups") or {}
    for group in GROUPS:
        reqs += [r for r in optional.get(group) or [] if isinstance(r, str)]
        reqs += [r for r in groups.get(group) or [] if isinstance(r, str)]
    out = []
    for req in reqs:
        req = req.strip()
        name = re.split(r"[\s\[<>=!~;@]", req, maxsplit=1)[0]
        if not name or "@" in req.split(";")[0]:  # direct URL references are not allowed
            continue
        if req not in out:
            out.append(req)
    return out


def recipe_specs(recipe, log):
    specs, files = [], []
    for value in recipe.get("install", []) + recipe.get("tools", []):
        parts = value.split()
        if parts[0] in ("-r", "--requirement") and len(parts) == 2:
            files.append(parts[1])
            continue
        for part in parts:
            if part.startswith("-") or _LOCAL_PROJECT.match(part) or "://" in part:
                log.append("dropped recipe item %r" % part)
            else:
                specs.append(part)
    return specs, files


def plan(src, recipe, work_dir, log):
    """The pip arguments that install the repository's dependencies."""
    args = []
    _, recipe_files = recipe_specs(recipe, [])
    for rel in list(REQUIREMENT_FILES) + recipe_files:
        copy = sanitize_requirements(src, rel, work_dir, log=log)
        if copy:
            args += ["-r", copy]
    args += pyproject_requirements(src)
    specs, _ = recipe_specs(recipe, log)
    args += specs
    return args


# --------------------------------------------------------------------------- build


def build(src, venv, recipe, index_url="", find_links=(), python=None):
    """Create the venv and install into it. Returns (ok, summary dict)."""
    started = time.time()
    log = []
    python = python or sys.executable
    work = venv + ".prep"
    os.makedirs(work, exist_ok=True)
    summary = {"ok": False, "version": SCRIPT_VERSION, "python": python, "installed": [],
               "log": log}
    proc = subprocess.run([python, "-m", "venv", "--system-site-packages", venv],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        log.append("venv failed: " + proc.stdout.decode(errors="replace")[-2000:])
        return False, summary
    args = plan(src, recipe, work, log)
    summary["installed"] = [a for a in args if a != "-r"]
    if args:
        exe = os.path.join(venv, "bin", "python")
        cmd = [exe, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
               "--no-warn-script-location"]
        if index_url:
            cmd += ["--index-url", index_url]
        elif find_links:
            cmd += ["--no-index"]
        for link in find_links:
            cmd += ["--find-links", link]
        proc = subprocess.run(cmd + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = proc.stdout.decode(errors="replace")
        with open(venv + ".pip.log", "w", encoding="utf-8") as fh:
            fh.write(output)
        if proc.returncode != 0:
            tail = [ln for ln in output.splitlines() if ln.strip()][-15:]
            log.append("pip install failed (exit %d):" % proc.returncode)
            log.extend(tail)
            return False, summary
    summary["ok"] = True
    summary["seconds"] = round(time.time() - started, 1)
    with open(os.path.join(venv, MARKER + ".tmp"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    os.replace(os.path.join(venv, MARKER + ".tmp"), os.path.join(venv, MARKER))
    return True, summary


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--src", required=True)
    p.add_argument("--venv")
    p.add_argument("--recipe", default="")
    p.add_argument("--index-url", default="")
    p.add_argument("--find-links", action="append", default=[])
    p.add_argument("--key", action="store_true", help="print the cache key and exit")
    p.add_argument("--tag", default="")
    a = p.parse_args(argv)
    recipe_text = ""
    if a.recipe and os.path.isfile(a.recipe):
        with open(a.recipe, encoding="utf-8", errors="replace") as fh:
            recipe_text = fh.read()
    if a.key:
        print(cache_key(a.src, recipe_text, a.tag))
        return 0
    if not a.venv:
        p.error("--venv is required")
    ok, summary = build(a.src, a.venv, parse_recipe(recipe_text), a.index_url, a.find_links)
    print("envprep %s: %d requirement(s)" % ("ok" if ok else "FAILED",
                                              len(summary["installed"])))
    for line in summary["log"][-15:]:
        print("  " + line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
