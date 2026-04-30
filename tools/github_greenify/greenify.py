# python .\greenify.py --start 2026-04-01 --end 2026-04-26 --min-per-day 4 --max-per-day 8 --repo-count 1 --keep-remote --author-name "Khayal Aghazada" --author-email "khayal.aghazada.x@gmail.com"


"""
Generate GitHub contribution activity with disposable repositories.

Important:
- This only affects your contribution graph when commits are pushed to GitHub.
- Use responsibly and follow GitHub Terms of Service.
"""

from __future__ import annotations

import argparse
import datetime as dt
import time
import pathlib
import random
import shutil
import subprocess
import sys
import tempfile
import os
from collections import defaultdict
from typing import Dict, List, Tuple


def run(cmd: List[str], cwd: pathlib.Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=check,
    )


def require_tool(name: str) -> None:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Missing required tool: {name}")


def get_git_config(key: str) -> str:
    result = run(["git", "config", "--global", key], check=False)
    return (result.stdout or "").strip()


def daterange(start: dt.date, end: dt.date) -> List[dt.date]:
    days = (end - start).days + 1
    return [start + dt.timedelta(days=i) for i in range(days)]


def build_commit_plan(
    start: dt.date,
    end: dt.date,
    min_per_day: int,
    max_per_day: int,
    skip_weekends: bool,
    seed: int | None,
) -> List[Tuple[dt.datetime, str]]:
    if min_per_day < 0 or max_per_day < 0:
        raise ValueError("Commit counts cannot be negative.")
    if min_per_day > max_per_day:
        raise ValueError("min-per-day cannot be larger than max-per-day.")
    if seed is not None:
        random.seed(seed)

    plan: List[Tuple[dt.datetime, str]] = []
    for day in daterange(start, end):
        if skip_weekends and day.weekday() >= 5:
            continue
        count = random.randint(min_per_day, max_per_day)
        for index in range(count):
            # Spread commits across the day.
            hour = random.randint(9, 22)
            minute = random.randint(0, 59)
            second = random.randint(0, 59)
            when = dt.datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=dt.timezone.utc)
            msg = f"chore: activity commit {day.isoformat()} #{index + 1}"
            plan.append((when, msg))
    plan.sort(key=lambda x: x[0])
    return plan


def init_repo(repo_dir: pathlib.Path, author_name: str, author_email: str) -> None:
    run(["git", "init", "-b", "main"], cwd=repo_dir)
    run(["git", "config", "user.name", author_name], cwd=repo_dir)
    run(["git", "config", "user.email", author_email], cwd=repo_dir)
    readme = repo_dir / "README.md"
    readme.write_text("# Disposable contribution repo\n", encoding="utf-8")
    run(["git", "add", "README.md"], cwd=repo_dir)
    run(["git", "commit", "-m", "chore: bootstrap repo"], cwd=repo_dir)


def write_and_commit(repo_dir: pathlib.Path, when: dt.datetime, message: str) -> None:
    log_file = repo_dir / "activity.log"
    line = f"{when.isoformat()} {message}\n"
    with log_file.open("a", encoding="utf-8") as f:
        f.write(line)
    run(["git", "add", "activity.log"], cwd=repo_dir)
    env = {
        "GIT_AUTHOR_DATE": when.isoformat(),
        "GIT_COMMITTER_DATE": when.isoformat(),
    }
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(repo_dir),
        env={**os.environ, **env},
        text=True,
        capture_output=True,
        check=True,
    )


def ensure_gh_auth() -> None:
    result = run(["gh", "auth", "status"], check=False)
    if result.returncode != 0:
        raise RuntimeError("GitHub CLI is not authenticated. Run: gh auth login")


def resolve_repo_name_with_owner(repo_name: str, repo_dir: pathlib.Path) -> str:
    if "/" in repo_name:
        return repo_name
    result = run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], cwd=repo_dir, check=False)
    resolved = (result.stdout or "").strip()
    if resolved and "/" in resolved:
        return resolved
    return repo_name


def delete_remote_repo(name: str, yes: bool) -> subprocess.CompletedProcess:
    args = ["gh", "repo", "delete", name]
    if yes:
        args.append("--yes")
    return run(args, check=False)


def split_plan_by_day(plan: List[Tuple[dt.datetime, str]], repo_count: int) -> List[List[Tuple[dt.datetime, str]]]:
    day_buckets: Dict[dt.date, List[Tuple[dt.datetime, str]]] = defaultdict(list)
    for item in plan:
        day_buckets[item[0].date()].append(item)

    grouped_by_day = [day_buckets[day] for day in sorted(day_buckets.keys())]
    chunks: List[List[Tuple[dt.datetime, str]]] = [[] for _ in range(repo_count)]
    for i, day_group in enumerate(grouped_by_day):
        chunks[i % repo_count].extend(day_group)
    return chunks


def build_repo_name(base_repo_name: str | None, index: int, total: int) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    suffix = f"{index + 1:03d}" if total > 1 else ""

    if base_repo_name:
        if total == 1:
            return base_repo_name
        if "/" in base_repo_name:
            owner, repo = base_repo_name.split("/", 1)
            return f"{owner}/{repo}-{suffix}"
        return f"{base_repo_name}-{suffix}"

    auto_base = f"greenify-temp-{stamp}"
    return f"{auto_base}-{suffix}" if suffix else auto_base


def build_retry_repo_name(repo_name: str, attempt: int) -> str:
    retry_tag = f"r{attempt:02d}-{random.randint(1000, 9999)}"
    if "/" in repo_name:
        owner, repo = repo_name.split("/", 1)
        return f"{owner}/{repo}-{retry_tag}"
    return f"{repo_name}-{retry_tag}"


def run_one_repo(
    repo_plan: List[Tuple[dt.datetime, str]],
    repo_name: str,
    visibility: str,
    auto_delete_remote: bool,
    keep_local: bool,
    create_retry: int,
    create_retry_delay: int,
    continue_on_error: bool,
    author_name: str,
    author_email: str,
) -> None:
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="greenify-"))
    print(f"Local repo: {temp_dir}")
    init_repo(temp_dir, author_name=author_name, author_email=author_email)

    for when, msg in repo_plan:
        write_and_commit(temp_dir, when, msg)

    current_repo_name = repo_name
    print(f"Creating {visibility} remote repo: {current_repo_name}")
    create_ok = False
    max_attempts = max(1, create_retry)
    for attempt in range(1, max_attempts + 1):
        create_result = run(
            ["gh", "repo", "create", current_repo_name, f"--{visibility}", "--source", ".", "--remote", "origin", "--push"],
            cwd=temp_dir,
            check=False,
        )
        if create_result.returncode == 0:
            create_ok = True
            print("Pushed commits successfully.")
            break
        stderr = (create_result.stderr or "").strip()
        stdout = (create_result.stdout or "").strip()
        print(f"Create failed (attempt {attempt}/{max_attempts}).")
        if stderr:
            print(stderr)
        elif stdout:
            print(stdout)
        if attempt < max_attempts:
            # If name was reserved/created during a partial failure, retry with a new name.
            current_repo_name = build_retry_repo_name(current_repo_name, attempt)
            print(f"Retrying with repo name: {current_repo_name}")
            print(f"Retrying in {create_retry_delay}s...")
            time.sleep(max(1, create_retry_delay))

    if not create_ok:
        if continue_on_error:
            print("Warning: skipping this repo after repeated create failures.")
        else:
            raise RuntimeError(f"Failed to create/push repository: {current_repo_name}")

    if create_ok and auto_delete_remote:
        repo_for_delete = resolve_repo_name_with_owner(current_repo_name, temp_dir)
        print(f"Deleting remote repo: {repo_for_delete}")
        delete_result = delete_remote_repo(repo_for_delete, yes=True)
        if delete_result.returncode == 0:
            print("Remote repo deleted.")
        else:
            stderr = (delete_result.stderr or "").strip()
            print("Warning: failed to delete remote repo automatically.")
            if stderr:
                print(stderr)

    if keep_local:
        print(f"Kept local repo at: {temp_dir}")
    else:
        shutil.rmtree(temp_dir, ignore_errors=True)
        print("Local temp directory removed.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill your GitHub contribution graph using disposable commits.")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--min-per-day", type=int, default=1, help="Minimum commits per day")
    parser.add_argument("--max-per-day", type=int, default=4, help="Maximum commits per day")
    parser.add_argument("--skip-weekends", action="store_true", help="Do not create commits on weekends")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for repeatable plans")
    parser.add_argument("--repo-count", type=int, default=1, help="Number of different repos to use")
    parser.add_argument("--repo-name", default=None, help="GitHub repo name (owner/name or name for your account)")
    parser.add_argument("--author-name", default=None, help="Commit author name (defaults to git global user.name)")
    parser.add_argument("--author-email", default=None, help="Commit author email (defaults to git global user.email)")
    parser.add_argument("--public", action="store_true", help="Create public repo (default private)")
    parser.add_argument(
        "--create-retry",
        type=int,
        default=3,
        help="How many times to retry GitHub repo creation per repo",
    )
    parser.add_argument(
        "--create-retry-delay",
        type=int,
        default=8,
        help="Seconds to wait between create retries",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue processing next repos if one repo fails",
    )
    parser.add_argument(
        "--auto-delete-remote",
        dest="delete_remote",
        action="store_true",
        help="Delete remote repo after push completes (default behavior)",
    )
    parser.add_argument(
        "--keep-remote",
        dest="delete_remote",
        action="store_false",
        help="Keep remote repositories instead of deleting them",
    )
    parser.set_defaults(delete_remote=True)
    parser.add_argument(
        "--keep-local",
        action="store_true",
        help="Keep local temp repo directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commit plan only; do not create or push anything",
    )
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()

        start = dt.date.fromisoformat(args.start)
        end = dt.date.fromisoformat(args.end)
        if start > end:
            raise ValueError("Start date must be <= end date.")
        if args.repo_count <= 0:
            raise ValueError("repo-count must be >= 1.")
        if args.create_retry <= 0:
            raise ValueError("create-retry must be >= 1.")
        if args.create_retry_delay <= 0:
            raise ValueError("create-retry-delay must be >= 1.")

        if end > dt.date.today():
            print(
                f"Warning: end date {end.isoformat()} is in the future. "
                "Future-day contributions usually appear only when those dates arrive."
            )

        plan = build_commit_plan(
            start=start,
            end=end,
            min_per_day=args.min_per_day,
            max_per_day=args.max_per_day,
            skip_weekends=args.skip_weekends,
            seed=args.seed,
        )

        print(f"Planned commits: {len(plan)}")
        if plan:
            print(f"First commit: {plan[0][0].isoformat()}")
            print(f"Last commit:  {plan[-1][0].isoformat()}")

        effective_repo_count = 1
        if plan:
            unique_days = sorted({when.date() for when, _ in plan})
            effective_repo_count = min(args.repo_count, len(unique_days))
            if args.repo_count > len(unique_days):
                print(
                    f"Requested {args.repo_count} repos, but only {len(unique_days)} active days in plan. "
                    f"Using {effective_repo_count} repos to keep day-based distribution."
                )
            print(f"Planned repositories: {effective_repo_count}")

        if args.dry_run:
            for when, msg in plan[:20]:
                print(f"- {when.isoformat()} | {msg}")
            if len(plan) > 20:
                print(f"... and {len(plan) - 20} more commits")
            return 0

        if not plan:
            print("No commits to generate for this date range/config.")
            return 0

        require_tool("git")
        require_tool("gh")
        ensure_gh_auth()
        author_name = args.author_name or get_git_config("user.name")
        author_email = args.author_email or get_git_config("user.email")
        if not author_name:
            raise ValueError(
                "No author name found. Set git global user.name or pass --author-name."
            )
        if not author_email:
            raise ValueError(
                "No author email found. Set git global user.email or pass --author-email."
            )
        print(f"Using commit author: {author_name} <{author_email}>")
        if args.delete_remote:
            print("Warning: auto-delete may remove contribution visibility for deleted repos.")

        visibility = "public" if args.public else "private"
        repo_plans = split_plan_by_day(plan, effective_repo_count)
        for i, repo_plan in enumerate(repo_plans):
            if not repo_plan:
                continue
            print(f"\n=== Repo {i + 1}/{effective_repo_count} ({len(repo_plan)} commits) ===")
            repo_name = build_repo_name(args.repo_name, i, effective_repo_count)
            run_one_repo(
                repo_plan=repo_plan,
                repo_name=repo_name,
                visibility=visibility,
                auto_delete_remote=args.delete_remote,
                keep_local=args.keep_local,
                create_retry=args.create_retry,
                create_retry_delay=args.create_retry_delay,
                continue_on_error=args.continue_on_error,
                author_name=author_name,
                author_email=author_email,
            )

        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
