"""approve_cli.py — Interactive terminal Human-in-the-Loop approval console.

A tiny operator tool that fulfills the "interactive terminal prompt" flavor of
the HITL gate. It talks to the running FastAPI service, shows the pending patch,
and asks the operator to approve or reject it.

Usage:
    python approve_cli.py --base-url http://localhost:8080 \
        --task-id <TASK_ID> --token <APPROVAL_TOKEN>

If --task-id / --token are omitted, you will be prompted for them.
"""

from __future__ import annotations

import argparse
import json
import sys

import requests


def _print_patch(patch: dict | None) -> None:
    if not patch:
        print("  (no patch attached)")
        return
    print(f"  summary    : {patch.get('summary', '')}")
    print(f"  root_cause : {patch.get('root_cause', '')}")
    print(f"  file_path  : {patch.get('file_path', '')}")
    print(f"  confidence : {patch.get('confidence', '')}")
    diff = patch.get("unified_diff", "")
    if diff:
        print("  --- unified diff ---")
        for line in diff.splitlines():
            print(f"  {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description="HITL approval console.")
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--token", default="")
    args = parser.parse_args()

    task_id = args.task_id or input("Task ID: ").strip()
    token = args.token or input("Approval token: ").strip()

    # Fetch and display current state.
    resp = requests.get(f"{args.base_url}/tasks/{task_id}", timeout=30)
    if resp.status_code != 200:
        print(f"Could not fetch task ({resp.status_code}): {resp.text}")
        return 1

    record = resp.json()
    print("\n=== Pending Task ===")
    print(f"  task_id : {record.get('task_id')}")
    print(f"  status  : {record.get('status')}")
    print(f"  repo    : {record.get('repo')}")
    print(f"  issue   : {record.get('issue_title')}")
    print("  --- proposed patch ---")
    _print_patch(record.get("patch"))

    choice = input("\nApprove this fix and create the PR? [y/N]: ").strip().lower()
    action = "approve" if choice in ("y", "yes") else "reject"

    body = {"task_id": task_id, "approval_token": token}
    if action == "reject":
        body["reason"] = input("Rejection reason (optional): ").strip()

    result = requests.post(f"{args.base_url}/workflow/{action}", json=body, timeout=60)
    print("\n=== Result ===")
    print(json.dumps(result.json(), indent=2))
    return 0 if result.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
