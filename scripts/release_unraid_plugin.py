#!/usr/bin/env python3
"""Build the CA Workshop Unraid plugin and publish/update a GitHub release.

Automates the full release process:
  1. Pre-flight syntax and environment checks (server.py, scripts, app.js, gh CLI).
  2. Auto-resolves repository metadata (owner/repo, current branch) and next version.
  3. Builds the Unraid plugin packages (canonical and versioned .plg files).
  4. Stages, commits, and pushes plugin updates to GitHub.
  5. Creates or updates the GitHub release with .plg assets and release notes.

Usage examples:
    # Auto-detect version for today, build, commit, push, and create release:
    python3 scripts/release_unraid_plugin.py

    # Specific version and custom notes:
    python3 scripts/release_unraid_plugin.py --version 2026.10.30 --notes "Added dashboard improvements"

    # Use GitHub's automatic release notes generator:
    python3 scripts/release_unraid_plugin.py --generate-notes

    # Build only (local verification without pushing or releasing):
    python3 scripts/release_unraid_plugin.py --build-only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
PLUGINS_DIR = ROOT / "plugins"
BUILD_SCRIPT = SCRIPTS_DIR / "build_unraid_plugin.py"


def run(cmd: list[str], cwd: Path = ROOT, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a shell command returning CompletedProcess."""
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        capture_output=capture,
        check=check,
    )


def detect_repo_info() -> tuple[str, str, str]:
    """Detect GitHub owner, repo name, and current branch from git remote."""
    proc = run(["git", "remote", "get-url", "origin"], capture=True, check=False)
    if proc.returncode != 0:
        raise SystemExit("Error: Could not determine git remote URL for 'origin'.")
    remote_url = proc.stdout.strip()

    match = re.search(r"(?:github\.com[:/])([^/]+)/([^/.]+)(?:\.git)?$", remote_url)
    if not match:
        raise SystemExit(f"Error: Unable to parse GitHub owner/repo from remote URL: {remote_url}")
    owner, repo = match.group(1), match.group(2)

    branch_proc = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture=True, check=False)
    branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 and branch_proc.stdout.strip() else "main"

    return owner, repo, branch


def get_existing_tags() -> set[str]:
    """Retrieve existing release tags from GitHub via gh CLI."""
    if not shutil.which("gh"):
        return set()
    proc = run(["gh", "release", "list", "--json", "tagName", "--limit", "100"], capture=True, check=False)
    if proc.returncode != 0:
        return set()
    try:
        data = json.loads(proc.stdout)
        return {item["tagName"] for item in data if "tagName" in item}
    except (ValueError, TypeError, KeyError):
        return set()


def suggest_next_version(existing_tags: set[str]) -> str:
    """Suggest the next sequential version for today's date."""
    today = dt.date.today().strftime("%Y.%m.%d")
    if f"v{today}" not in existing_tags and today not in existing_tags:
        return today
    for ch in "bcdefghijklmnopqrstuvwxyz":
        candidate = f"{today}{ch}"
        if f"v{candidate}" not in existing_tags and candidate not in existing_tags:
            return candidate
    return f"{today}.{int(dt.datetime.now().timestamp())}"


def run_checks():
    """Run syntax checks on Python files and app.js."""
    print("▶ Running pre-flight checks…")
    py_files = [str(ROOT / "server.py"), str(BUILD_SCRIPT), str(Path(__file__))]
    compile_cmd = [sys.executable, "-m", "py_compile", *py_files]
    res = run(compile_cmd, capture=True, check=False)
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        raise SystemExit("Pre-flight check failed: Python syntax error.")

    if shutil.which("node"):
        js_file = ROOT / "app.js"
        if js_file.is_file():
            res = run(["node", "-c", str(js_file)], capture=True, check=False)
            if res.returncode != 0:
                sys.stderr.write(res.stderr)
                raise SystemExit("Pre-flight check failed: JavaScript syntax error in app.js.")
    print("✓ Syntax checks passed.")


def verify_gh_cli():
    """Verify GitHub CLI is installed and authenticated."""
    if not shutil.which("gh"):
        raise SystemExit("Error: 'gh' (GitHub CLI) is not installed. Install it from https://cli.github.com/ and login with 'gh auth login'.")
    res = run(["gh", "auth", "status"], capture=True, check=False)
    if res.returncode != 0:
        sys.stderr.write(res.stderr or res.stdout)
        raise SystemExit("Error: 'gh' CLI is not authenticated. Run 'gh auth login' first.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--version", default="", help="Plugin version (e.g. 2026.10.30). Defaults to next available for today.")
    parser.add_argument("-t", "--title", default="", help="Release title. Defaults to 'CA Workshop v<version>'.")
    parser.add_argument("-m", "--notes", default="", help="Release notes text.")
    parser.add_argument("-F", "--notes-file", type=Path, default=None, help="Path to markdown file containing release notes.")
    parser.add_argument("--generate-notes", action="store_true", help="Automatically generate release notes from git commits via GitHub.")
    parser.add_argument("--repo", default="", help="GitHub repository in owner/repo format. Auto-detected by default.")
    parser.add_argument("--branch", default="", help="Branch for canonical plugin URL. Defaults to current branch.")
    parser.add_argument("--build-only", action="store_true", help="Build plugin package locally without committing or releasing.")
    parser.add_argument("--skip-checks", action="store_true", help="Skip pre-flight syntax checks.")
    parser.add_argument("--skip-commit", action="store_true", help="Skip git add/commit/push steps before releasing.")
    parser.add_argument("--draft", action="store_true", help="Create GitHub release as a draft.")
    parser.add_argument("--prerelease", action="store_true", help="Mark GitHub release as a prerelease.")
    args = parser.parse_args()

    owner, repo_name, current_branch = detect_repo_info()
    target_repo = args.repo or f"{owner}/{repo_name}"
    target_branch = args.branch or current_branch

    if not args.skip_checks:
        run_checks()

    if not args.build_only:
        verify_gh_cli()

    existing_tags = get_existing_tags()

    version = args.version.lstrip("v").strip()
    if not version:
        version = suggest_next_version(existing_tags)
        print(f"▶ Auto-selected next version: {version}")
    else:
        print(f"▶ Target version: {version}")

    tag_name = f"v{version}"
    canonical_plg_url = f"https://raw.githubusercontent.com/{target_repo}/{target_branch}/plugins/ca-workshop.plg"
    support_url = f"https://github.com/{target_repo}"

    # 1. Build the plugin
    print(f"▶ Building plugin package for version {version}…")
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    build_cmd = [
        sys.executable,
        str(BUILD_SCRIPT),
        "--version", version,
        "--plugin-url", canonical_plg_url,
        "--support-url", support_url,
        "--output", str(PLUGINS_DIR),
    ]
    run(build_cmd, check=True)

    versioned_plg = PLUGINS_DIR / f"ca-workshop-{version}.plg"
    canonical_plg = PLUGINS_DIR / "ca-workshop.plg"

    if not versioned_plg.is_file() or not canonical_plg.is_file():
        raise SystemExit("Error: Expected build artifacts were not created.")

    print(f"✓ Built: {versioned_plg.name} ({versioned_plg.stat().st_size:,} bytes)")
    print(f"✓ Built: {canonical_plg.name} ({canonical_plg.stat().st_size:,} bytes)")

    if args.build_only:
        print("\n✔ Build complete (--build-only mode). Nothing pushed or released.")
        return 0

    # 2. Git stage, commit, and push
    if not args.skip_commit:
        print("▶ Checking git status…")
        run(["git", "add", str(versioned_plg), str(canonical_plg)], check=True)

        # Check if there are changes to commit
        status_proc = run(["git", "diff", "--staged", "--name-only"], capture=True, check=True)
        staged_files = [f for f in status_proc.stdout.splitlines() if f.strip()]

        if staged_files:
            commit_msg = f"Release {tag_name}"
            print(f"▶ Committing {len(staged_files)} staged file(s): {commit_msg}")
            run(["git", "commit", "-m", commit_msg], check=True)
        else:
            print("✓ No git changes to commit.")

        print(f"▶ Pushing commits to origin/{target_branch}…")
        run(["git", "push", "origin", target_branch], check=True)
        print("✓ Pushed to GitHub.")

    # 3. Create or update GitHub release
    title = args.title or f"CA Workshop {tag_name}"
    release_exists = tag_name in existing_tags

    if release_exists:
        print(f"▶ Release {tag_name} already exists. Uploading updated assets…")
        upload_cmd = [
            "gh", "release", "upload", tag_name,
            str(versioned_plg), str(canonical_plg),
            "--clobber",
            "--repo", target_repo,
        ]
        run(upload_cmd, check=True)

        edit_cmd = ["gh", "release", "edit", tag_name, "--repo", target_repo]
        if args.title:
            edit_cmd.extend(["--title", title])
        if args.notes:
            edit_cmd.extend(["--notes", args.notes])
        elif args.notes_file and args.notes_file.is_file():
            edit_cmd.extend(["--notes-file", str(args.notes_file)])

        if len(edit_cmd) > 4:
            run(edit_cmd, check=True)
        print(f"✓ Updated existing release {tag_name}.")
    else:
        print(f"▶ Creating GitHub release {tag_name}…")
        create_cmd = [
            "gh", "release", "create", tag_name,
            str(versioned_plg), str(canonical_plg),
            "--title", title,
            "--repo", target_repo,
        ]
        if args.generate_notes:
            create_cmd.append("--generate-notes")
        elif args.notes_file and args.notes_file.is_file():
            create_cmd.extend(["--notes-file", str(args.notes_file)])
        elif args.notes:
            create_cmd.extend(["--notes", args.notes])
        else:
            default_notes = (
                f"### CA Workshop {tag_name}\n\n"
                f"Community Applications repository maintainer workbench for Unraid.\n\n"
                f"#### Install or Update on Unraid\n"
                f"```\n{canonical_plg_url}\n```\n"
            )
            create_cmd.extend(["--notes", default_notes])

        if args.draft:
            create_cmd.append("--draft")
        if args.prerelease:
            create_cmd.append("--prerelease")

        run(create_cmd, check=True)
        print(f"✓ Created release {tag_name}.")

    release_url = f"https://github.com/{target_repo}/releases/tag/{tag_name}"
    print("\n" + "=" * 70)
    print(f"🎉 CA Workshop {tag_name} released successfully!")
    print(f"• GitHub Release: {release_url}")
    print(f"• Unraid Plugin URL: {canonical_plg_url}")
    print("=" * 70)
    print("\nTo install or update on Unraid (WebGUI: Plugins → Install Plugin):")
    print(f"  {canonical_plg_url}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
