#!/usr/bin/env python3
"""Sync repository Agent Skills into Claude Code's project skill directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".agents" / "skills"
TARGET = ROOT / ".claude" / "skills"
MANIFEST = TARGET / ".adx-project-skills.json"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SyncError(RuntimeError):
    pass


def frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise SyncError(f"{path} must start with YAML frontmatter")

    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return values
        if ":" in line and not line.startswith((" ", "\t")):
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    raise SyncError(f"{path} has no closing frontmatter delimiter")


def discover() -> dict[str, Path]:
    if not SOURCE.is_dir():
        raise SyncError(f"Missing canonical skill directory: {SOURCE}")

    result: dict[str, Path] = {}
    for directory in sorted(path for path in SOURCE.iterdir() if path.is_dir()):
        if directory.is_symlink():
            raise SyncError(f"Canonical skills cannot be symlinks: {directory}")

        skill_file = directory / "SKILL.md"
        if not skill_file.is_file():
            raise SyncError(f"Missing SKILL.md: {directory}")

        metadata = frontmatter(skill_file)
        name = metadata.get("name", "")
        description = metadata.get("description", "")
        if (
            name != directory.name
            or not NAME_RE.fullmatch(name)
            or not 1 <= len(name) <= 64
        ):
            raise SyncError(f"Invalid or mismatched skill name: {directory}")
        if not description or len(description) > 1024:
            raise SyncError(f"Invalid skill description: {skill_file}")

        result[name] = directory
    return result


def managed_names() -> set[str]:
    if not MANIFEST.is_file():
        return set()
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        names = data["managed_skills"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SyncError(f"Invalid sync manifest: {MANIFEST}") from exc
    if not isinstance(names, list) or not all(
        isinstance(name, str) and NAME_RE.fullmatch(name) for name in names
    ):
        raise SyncError(f"Invalid sync manifest: {MANIFEST}")
    return set(names)


def target_for(name: str) -> Path:
    target = (TARGET / name).resolve()
    if target.parent != TARGET.resolve():
        raise SyncError(f"Unsafe target path: {target}")
    return target


def hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def write(skills: dict[str, Path], force: bool) -> None:
    if TARGET.is_symlink():
        raise SyncError(f"Generated skill root cannot be a symlink: {TARGET}")
    TARGET.mkdir(parents=True, exist_ok=True)

    previous = managed_names()
    current = set(skills)

    collisions = [
        name
        for name in current
        if target_for(name).exists() and name not in previous and not force
    ]
    if collisions:
        raise SyncError(
            "Unmanaged Claude skill collision: "
            + ", ".join(sorted(collisions))
            + ". Review it before using --force."
        )

    for name in sorted(previous - current):
        target = target_for(name)
        if target.exists():
            shutil.rmtree(target)

    for name, source in skills.items():
        target = target_for(name)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)

    MANIFEST.write_text(
        json.dumps({"managed_skills": sorted(current)}, indent=2) + "\n",
        encoding="utf-8",
    )


def check(skills: dict[str, Path]) -> None:
    if managed_names() != set(skills):
        raise SyncError("Skill mirror is stale; run with --write")
    for name, source in skills.items():
        target = target_for(name)
        if not target.is_dir() or hashes(source) != hashes(target):
            raise SyncError(f"Skill mirror differs from canonical source: {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    try:
        skills = discover()
        if args.write:
            write(skills, args.force)
            check(skills)
            print(f"Synchronized {len(skills)} project skill(s)")
        else:
            check(skills)
            print(f"Verified {len(skills)} project skill(s)")
    except SyncError as exc:
        print(f"skill sync error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
