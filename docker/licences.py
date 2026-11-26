"""Licence report for the images (handoff M12): what is inside, under which licence.

    python docker/licences.py --python            installed Python distributions
    python docker/licences.py --go agent/vendor   vendored Go modules
    python docker/licences.py --check FILE...     exit 1 if any row needs a look

Output is TSV: name, version, licence, verdict (`ok`, `review`: copyleft or not
identified). Standard library only.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PERMISSIVE = ("MIT", "BSD", "Apache", "ISC", "PSF", "Python Software Foundation", "MPL",
              "Unlicense", "Zlib", "0BSD", "CC0", "HPND")
COPYLEFT = re.compile(r"\b(A?GPL|LGPL|General Public License|EUPL|SSPL)\b", re.I)


def verdict(licence: str) -> str:
    if licence.startswith("ours"):
        return "ok"
    if not licence or licence == "?" or COPYLEFT.search(licence):
        return "review"
    return "ok" if any(p.lower() in licence.lower() for p in PERMISSIVE) else "review"


def python_rows() -> list[tuple[str, str, str]]:
    from importlib import metadata

    rows = []
    for dist in metadata.distributions():
        meta = dist.metadata
        lic = meta.get("License-Expression") or ""
        if not lic:
            classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
                           if c.startswith("License ::")]
            declared = (meta.get("License") or "").strip().splitlines()
            lic = "; ".join(classifiers) or (declared[0][:60] if declared else "")
        if meta["Name"] == "habodek":
            lic = "ours (see NOTICE for what it adapts)"
        rows.append((meta["Name"], dist.version, lic or "?"))
    return sorted(set(rows), key=lambda r: r[0].lower())


LICENCE_TEXTS = (
    (r"Apache License", "Apache-2.0"),
    (r"MIT License|Permission is hereby granted, free of charge", "MIT"),
    (r"Mozilla Public License", "MPL-2.0"),
    (r"ISC License|Permission to use, copy, modify, and/or distribute", "ISC"),
    (r"GNU (Lesser|Library) General Public", "LGPL"),
    (r"GNU General Public", "GPL"),
    (r"Redistribution and use in source and binary forms", "BSD"),
)


def classify_text(text: str) -> str:
    head = text[:3000]
    for pattern, name in LICENCE_TEXTS:
        if re.search(pattern, head, re.I):
            return name
    return "?"


def go_rows(vendor: Path) -> list[tuple[str, str, str]]:
    rows = []
    for line in (vendor / "modules.txt").read_text().splitlines():
        m = re.match(r"# (\S+) (\S+)", line)
        if not m:
            continue
        module, version = m.groups()
        licence = "?"
        root = vendor / module
        candidates = (sorted(root.glob("LICEN[CS]E*")) + sorted(root.glob("COPYING*"))
                      or sorted(root.rglob("LICEN[CS]E*")))  # only subpackages vendored
        for candidate in candidates:
            licence = classify_text(candidate.read_text(errors="replace"))
            break
        if licence == "?" and module == "github.com/stretchr/testify":
            licence = "MIT (test-only; licence file not vendored)"
        rows.append((module, version, licence))
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--python", action="store_true")
    p.add_argument("--go", metavar="VENDOR_DIR")
    p.add_argument("--check", nargs="+", metavar="TSV")
    a = p.parse_args(argv)
    if a.check:
        flagged = [line for f in a.check for line in Path(f).read_text().splitlines()
                   if line.endswith("\treview")]
        for line in flagged[:15]:
            print("review:", line.replace("\t", "  "))
        return 1 if flagged else 0
    rows = python_rows() if a.python else go_rows(Path(a.go)) if a.go else []
    for name, version, lic in rows:
        print(f"{name}\t{version}\t{lic}\t{verdict(lic)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
