#!/usr/bin/env python

import csv
import os
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from mcp.server import MCPServer

mcp = MCPServer("tracker")

# Constants

FILEPATH = str(
    Path(os.environ.get("TRACKER_FILE", Path(__file__).resolve().parent / "data" / "tasks.csv"))
    .expanduser()
    .resolve()
)
FIELDNAMES = [
    "id", "type", "title", "category", "amount",
    "status", "due_date", "created_at", "updated_at",
]
VALID_TYPES = ("task", "expense")
VALID_STATUSES = ("pending", "in_progress", "completed", "cancelled")


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# Layer 1: Data access

def ensure_storage_exists(filepath: str = FILEPATH) -> None:
    directory = os.path.dirname(filepath)
    if directory:
        os.makedirs(directory, exist_ok=True)
    if not os.path.exists(filepath):
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()


def read_all_records(filepath: str = FILEPATH) -> list[dict]:
    ensure_storage_exists(filepath)
    with open(filepath, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_all_records(records: list[dict], filepath: str = FILEPATH) -> None:
    tmp_path = filepath + ".tmp"
    try:
        with open(tmp_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# Layer 2: Business logic

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _resolve_id(records: list[dict], record_id: str) -> dict:
    """Accept a full UUID or a unique prefix; raise ValueError otherwise."""
    prefix = (record_id or "").strip().lower()
    if not prefix:
        raise ValueError("record_id is required")
    matches = [r for r in records if r["id"].lower().startswith(prefix)]
    if not matches:
        raise ValueError(f"no record found with id '{record_id}'")
    if len(matches) > 1:
        raise ValueError(f"id prefix '{record_id}' is ambiguous ({len(matches)} matches)")
    return matches[0]


def _apply_updates(record: dict, updates: dict) -> None:
    candidate = dict(record)
    for key, value in updates.items():
        if key in ("id", "created_at", "updated_at") or key not in FIELDNAMES:
            continue
        candidate[key] = value

    if candidate["type"] not in VALID_TYPES:
        raise ValueError(f"type must be one of {VALID_TYPES}")
    if candidate["status"] not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}")
    if not str(candidate["title"]).strip():
        raise ValueError("title cannot be empty")
    try:
        if float(candidate["amount"] or 0) < 0:
            raise ValueError
    except ValueError:
        raise ValueError("amount must be a non-negative number")
    if candidate["due_date"] and not _validate_date(candidate["due_date"]):
        raise ValueError("due_date must be in YYYY-MM-DD format")

    candidate["updated_at"] = _now_iso()
    record.update(candidate)


# MCP tools

@mcp.tool()
def add_record(record_type: str, title: str, category: str = "general",
               amount: float = 0.0, due_date: str = "") -> dict:
    """Add a new task or expense.

    record_type: 'task' or 'expense'. due_date: YYYY-MM-DD or empty.
    Returns the created record.
    """
    record_type = (record_type or "").strip().lower()
    title = (title or "").strip()
    category = (category or "").strip().lower()

    if record_type not in VALID_TYPES:
        raise ValueError(f"record_type must be one of {VALID_TYPES}")
    if not title:
        raise ValueError("title cannot be empty")
    if amount < 0:
        raise ValueError("amount must be >= 0")
    if due_date and not _validate_date(due_date):
        raise ValueError("due_date must be in YYYY-MM-DD format")

    now = _now_iso()
    record = {
        "id": str(uuid.uuid4()),
        "type": record_type,
        "title": title,
        "category": category,
        "amount": f"{amount:.2f}",
        "status": "pending",
        "due_date": due_date,
        "created_at": now,
        "updated_at": now,
    }
    records = read_all_records()
    records.append(record)
    write_all_records(records)
    return record


@mcp.tool()
def list_records(record_type: str = "", status: str = "", category: str = "") -> list[dict]:
    """List records. Leave a filter empty to skip it.

    record_type: 'task' / 'expense'. status: pending / in_progress /
    completed / cancelled.
    """
    result = []
    for r in read_all_records():
        if record_type and r["type"] != record_type.lower():
            continue
        if status and r["status"] != status.lower():
            continue
        if category and r["category"].lower() != category.lower():
            continue
        result.append(r)
    return result


@mcp.tool()
def update_record(record_id: str, title: str | None = None,
                  category: str | None = None, status: str | None = None,
                  due_date: str | None = None, amount: float | None = None) -> dict:
    """Update fields of a record. Only the fields you pass are changed.

    record_id may be the full UUID or a unique prefix.
    """
    records = read_all_records()
    record = _resolve_id(records, record_id)
    updates = {}
    if title is not None:
        updates["title"] = title.strip()
    if category is not None:
        updates["category"] = category.strip().lower()
    if status is not None:
        updates["status"] = status.strip().lower()
    if due_date is not None:
        updates["due_date"] = due_date.strip()
    if amount is not None:
        updates["amount"] = f"{amount:.2f}"
    _apply_updates(record, updates)
    write_all_records(records)
    return record


@mcp.tool()
def complete_record(record_id: str) -> dict:
    """Mark a record as completed (full UUID or unique prefix)."""
    records = read_all_records()
    record = _resolve_id(records, record_id)
    _apply_updates(record, {"status": "completed"})
    write_all_records(records)
    return record


@mcp.tool()
def delete_record(record_id: str) -> str:
    """Permanently delete a record (full UUID or unique prefix)."""
    records = read_all_records()
    record = _resolve_id(records, record_id)
    records = [r for r in records if r["id"] != record["id"]]
    write_all_records(records)
    return f"Deleted '{record['title']}' ({record['id'][:8]})."


@mcp.tool()
def get_summary() -> dict:
    """Totals: expense total, pending/completed task counts, spend by category."""
    total = 0.0
    by_category: dict[str, float] = defaultdict(float)
    pending = completed = 0

    for r in read_all_records():
        if r["type"] == "expense" and r["status"] != "cancelled":
            try:
                amt = float(r.get("amount") or 0)
            except ValueError:
                amt = 0.0
            total += amt
            by_category[r.get("category") or "uncategorized"] += amt
        elif r["type"] == "task":
            if r["status"] == "pending":
                pending += 1
            elif r["status"] == "completed":
                completed += 1

    return {
        "total_expenses": round(total, 2),
        "pending_tasks": pending,
        "completed_tasks": completed,
        "spend_by_category": {k: round(v, 2) for k, v in by_category.items()},
    }


if __name__ == "__main__":
    ensure_storage_exists()
    mcp.run() 