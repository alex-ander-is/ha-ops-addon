#!/usr/bin/env python3
"""Compute and verify HA Ops release versions against an explicit impact type."""

import argparse
import re
import subprocess
import sys


VERSION_PATH = "ha-ops/config.yaml"
CHANGELOG_PATH = "ha-ops/CHANGELOG.md"
KINDS = ("major", "minor", "patch")
VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
CONFIG_VERSION_RE = re.compile(r'^version: "([^"]+)"$', re.MULTILINE)


class ReleaseError(ValueError):
    pass


def run_git(*args):
    result = subprocess.run(["git", *args], text=True, capture_output=True)
    if result.returncode:
        raise ReleaseError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def parse_version(value):
    match = VERSION_RE.fullmatch(value)
    if not match:
        raise ReleaseError(f"invalid version {value!r}; expected X.Y.Z")
    return tuple(int(part) for part in match.groups())


def next_version(current, kind):
    major, minor, patch = parse_version(current)
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ReleaseError(f"unknown release type {kind!r}")


def version_at(revision):
    content = run_git("show", f"{revision}:{VERSION_PATH}")
    matches = CONFIG_VERSION_RE.findall(content)
    if len(matches) != 1:
        raise ReleaseError(f"expected one quoted version in {VERSION_PATH} at {revision}")
    parse_version(matches[0])
    return matches[0]


def trailer(message, name):
    matches = re.findall(rf"^{re.escape(name)}: ([^\n]+)$", message, re.MULTILINE)
    if len(matches) != 1 or not matches[0].strip():
        raise ReleaseError(f"release commit must have exactly one nonempty {name} trailer")
    return matches[0].strip()


def changelog_section(text, version):
    headings = list(re.finditer(r"^## ([0-9]+\.[0-9]+\.[0-9]+)$", text, re.MULTILINE))
    matches = [(index, match) for index, match in enumerate(headings) if match.group(1) == version]
    if len(matches) != 1:
        raise ReleaseError(f"expected exactly one changelog section for {version}")
    index, match = matches[0]
    end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
    return index, text[match.end():end]


def check_release(base, head):
    old = version_at(base)
    new = version_at(head)
    message = run_git("log", "-1", "--format=%B", head)
    kind = trailer(message, "Release-Type")
    if kind not in KINDS:
        raise ReleaseError(f"Release-Type must be one of {', '.join(KINDS)}")
    trailer(message, "Release-Impact")
    expected = next_version(old, kind)
    if new != expected:
        raise ReleaseError(f"{kind} release from {old} must be {expected}, not {new}")

    old_changelog = run_git("show", f"{base}:{CHANGELOG_PATH}")
    new_changelog = run_git("show", f"{head}:{CHANGELOG_PATH}")
    new_index, section = changelog_section(new_changelog, new)
    old_index, old_section = changelog_section(new_changelog, old)
    if new_index >= old_index or not re.search(r"^[-*] \S", section, re.MULTILINE):
        raise ReleaseError(f"{CHANGELOG_PATH} needs a nonempty {new} section before {old}")
    _, prior_section = changelog_section(old_changelog, old)
    if old_section != prior_section:
        raise ReleaseError("the previous released changelog section changed")

    if run_git("cat-file", "-t", f"refs/tags/{new}") != "tag":
        raise ReleaseError(f"tag {new} must be annotated")
    if run_git("rev-parse", f"refs/tags/{new}^{{}}") != run_git("rev-parse", head):
        raise ReleaseError(f"tag {new} must point to the release commit")
    return old, new, kind


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    next_parser = subparsers.add_parser("next", help="calculate the next version")
    next_parser.add_argument("current")
    next_parser.add_argument("kind", choices=KINDS)
    check_parser = subparsers.add_parser("check", help="verify a committed release against its base")
    check_parser.add_argument("base")
    check_parser.add_argument("head")
    args = parser.parse_args()
    if args.command == "next":
        print(next_version(args.current, args.kind))
    else:
        old, new, kind = check_release(args.base, args.head)
        print(f"Verified {kind} release: {old} -> {new}")


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as error:
        raise SystemExit(f"Release policy: {error}")
