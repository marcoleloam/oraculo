#!/usr/bin/env python3
"""Check inactive CI name contracts and byte-exact public workflow exports."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

INACTIVE_HEADER = (
    b"# Export ONLY to a reviewed, sanitized public checkout. This file is inactive.\n"
)
# template, optional public destination, workflow name, job id, job name, header
CONTRACTS = (
    (
        "product-review.yml.template",
        "product-review.yml",
        "Oraculo product review",
        "product-review",
        "Product review (exact HEAD)",
        INACTIVE_HEADER,
    ),
    (
        "oraculo-suite.yml.template",
        "ci.yml",
        "CI",
        "basic",
        "Basic checks",
        INACTIVE_HEADER,
    ),
    (
        "oraculo-ci.yml.template",
        "orchestration.yml",
        "Oraculo offline validation",
        "offline",
        "Orchestration offline gate",
        INACTIVE_HEADER,
    ),
    (
        "full-ci.yml.template",
        "full-ci.yml",
        "Full CI",
        "tests",
        "Tests (${{ matrix.os }})",
        b"# Inactive. Export only to a reviewed sanitized public checkout as "
        b".github/workflows/full-ci.yml.\n",
    ),
)


def canonical_export(template, workflow_name, job_id, job_name, header):
    """Remove only the inactive header; never inject or normalize YAML fields.

    This checks the narrow, literal template name contract, not arbitrary YAML.
    Active workflows must then match these canonical bytes in their entirety.
    """
    if not template.startswith(header):
        raise ValueError("missing exact inactive template header")
    exported = template[len(header) :]
    text = exported.decode("utf-8")
    for indent, expected in (("", workflow_name), ("    ", job_name)):
        names = re.findall(
            rf"^{indent}(?:name|'name'|\"name\")[ \t]*:[^\n]*$",
            text,
            re.MULTILINE,
        )
        if names != [f"{indent}name: {expected}"]:
            raise ValueError("workflow and job names must occur exactly once")
    if (
        text.splitlines().count("jobs:") != 1
        or text.splitlines().count(f"  {job_id}:") != 1
        or f"  {job_id}:\n    name: {job_name}\n" not in text
    ):
        raise ValueError("missing exact job name binding")
    return exported


def validate_exports(root):
    checked = []
    for template, target, workflow_name, job_id, job_name, header in CONTRACTS:
        source = Path("templates/ci") / template
        try:
            expected = canonical_export(
                (root / source).read_bytes(), workflow_name, job_id, job_name, header
            )
        except (OSError, ValueError) as exc:
            raise ValueError(f"{source}: {exc}") from exc
        checked.append(str(source))
        if target is None:
            continue
        destination = Path(".github/workflows") / target
        path = root / destination
        if path.exists() or path.is_symlink():
            if not path.is_file() or path.read_bytes() != expected:
                raise ValueError(f"{destination}: differs from canonical {source}")
            checked.append(str(destination))
    return checked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    print(json.dumps({"state": "passed", "checked": validate_exports(args.root)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
