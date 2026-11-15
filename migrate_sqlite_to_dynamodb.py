#!/usr/bin/env python3
"""Migrate legacy SQLite task DBs (per-user ``{user}.db`` + global ``tasks.db``) to DynamoDB.

The migration is idempotent and merge-safe, so it can be re-run (e.g. a final delta
run right before cutover) and can be fed several copies of the same user's DB:

* tasks    : the copy with the newest ``updated_at`` wins (never overwritten by an older one)
* messages : inserted only if the message id does not exist yet (existing rows win)
* logins   : keyed by event id, inserted once
* ``message_count`` on every touched task is recomputed at the end

Examples::

    # local files (session_storage + working copies)
    python migrate_sqlite_to_dynamodb.py --source application/data --source /mnt/app-data --dry-run

    # also pull every S3 version of the DBs (recovers copies that were overwritten by stale syncs)
    python migrate_sqlite_to_dynamodb.py --s3-bucket my-bucket --s3-include-versions

Point at DynamoDB Local with ``DYNAMODB_ENDPOINT_URL=http://localhost:8000``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from botocore.exceptions import ClientError  # noqa: E402

from application import task_store as ts  # noqa: E402

logger = logging.getLogger("migrate")

SQLITE_MAGIC = b"SQLite format 3\x00"


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def discover_local(sources: list[str]) -> list[Path]:
    found: list[Path] = []
    for src in sources:
        root = Path(src).expanduser()
        if root.is_file():
            candidates: Iterator[Path] = iter([root])
        else:
            candidates = root.rglob("*.db")
        for path in candidates:
            if path.is_file() and path.stat().st_size > 0 and _is_sqlite(path):
                found.append(path)
    return sorted(set(found))


def download_s3(bucket: str, prefix: str, include_versions: bool, dest: Path) -> list[Path]:
    import boto3

    s3 = boto3.client("s3", region_name=ts._region())
    out: list[Path] = []
    n = 0

    def fetch(key: str, version_id: str | None) -> None:
        nonlocal n
        n += 1
        target = dest / f"s3_{n:05d}_{Path(key).name}"
        extra = {"VersionId": version_id} if version_id else None
        try:
            s3.download_file(bucket, key, str(target), ExtraArgs=extra)
        except ClientError as exc:
            logger.warning("skip s3://%s/%s (%s): %s", bucket, key, version_id, exc)
            return
        if target.stat().st_size > 0 and _is_sqlite(target):
            out.append(target)

    if include_versions:
        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for v in page.get("Versions", []):
                if v["Key"].endswith(".db"):
                    fetch(v["Key"], v["VersionId"])
    else:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(".db"):
                    fetch(obj["Key"], None)
    logger.info("downloaded %d SQLite file(s) from s3://%s/%s", len(out), bucket, prefix)
    return out


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _json_list(raw: Any) -> list[Any]:
    try:
        value = json.loads(raw) if raw else []
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def migrate_file(path: Path, *, dry_run: bool, touched: dict[tuple[str, str], int]) -> dict[str, int]:
    stats = defaultdict(int)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if _has_table(conn, "tasks"):
            tcols = _columns(conn, "tasks")
            task_owner: dict[str, str] = {}
            for row in conn.execute("SELECT * FROM tasks"):
                uid, tid = row["user_id"], row["id"]
                task_owner[tid] = uid
                item = {
                    "user_id": uid,
                    "task_id": tid,
                    "title": row["title"] or "New task",
                    "runtime_session_id": row["runtime_session_id"] or tid,
                    "model_name": row["model_name"] or ts.DEFAULT_MODEL,
                    "skills": [str(s) for s in _json_list(row["skills_json"])],
                    "mcp_servers": [str(s) for s in _json_list(row["mcp_servers_json"])],
                    "guardrail_enabled": bool(row["guardrail_enabled"]),
                    "memory_enabled": bool(row["memory_enabled"]) if "memory_enabled" in tcols else True,
                    "llm_gateway_enabled": bool(row["llm_gateway_enabled"]) if "llm_gateway_enabled" in tcols else False,
                    "pinned": bool(row["pinned"]) if "pinned" in tcols else False,
                    "message_count": 0,
                    "created_at": row["created_at"] or ts._now_iso(),
                    "updated_at": row["updated_at"] or row["created_at"] or ts._now_iso(),
                }
                stats["tasks_seen"] += 1
                touched[(uid, tid)] = 0
                if dry_run:
                    continue
                try:
                    ts._tasks().put_item(
                        Item=item,
                        ConditionExpression="attribute_not_exists(task_id) OR updated_at < :u",
                        ExpressionAttributeValues={":u": item["updated_at"]},
                    )
                    stats["tasks_written"] += 1
                except ClientError as exc:
                    if not ts._is_conditional_failure(exc):
                        raise
                    stats["tasks_skipped_newer"] += 1

            if _has_table(conn, "messages"):
                mcols = _columns(conn, "messages")
                for row in conn.execute("SELECT * FROM messages"):
                    uid = task_owner.get(row["task_id"])
                    if not uid:
                        stats["messages_orphan"] += 1
                        continue
                    content, images_json, events_json = ts._fit_payload(
                        row["content"] or "",
                        _json_list(row["images_json"]),
                        _json_list(row["tool_events_json"]),
                    )
                    item = {
                        "task_id": row["task_id"],
                        "message_id": row["id"],
                        "user_id": uid,
                        "role": row["role"],
                        "content": content,
                        "images_json": images_json,
                        "tool_events_json": events_json,
                        "status": (row["status"] if "status" in mcols and row["status"] else "complete"),
                        "created_at": row["created_at"] or ts._now_iso(),
                    }
                    stats["messages_seen"] += 1
                    if dry_run:
                        continue
                    try:
                        ts._messages().put_item(
                            Item=item, ConditionExpression="attribute_not_exists(message_id)"
                        )
                        stats["messages_written"] += 1
                    except ClientError as exc:
                        if not ts._is_conditional_failure(exc):
                            raise
                        stats["messages_exists"] += 1

        if _has_table(conn, "login_events"):
            for row in conn.execute("SELECT * FROM login_events"):
                stats["logins_seen"] += 1
                if dry_run:
                    continue
                item = {
                    "user_id": row["user_id"],
                    "sk": f"{row['logged_at']}#{row['id']}",
                    "id": row["id"],
                    "method": row["method"],
                    "logged_at": row["logged_at"],
                }
                if row["name"]:
                    item["name"] = row["name"]
                if row["picture"]:
                    item["picture"] = row["picture"]
                try:
                    ts._logins().put_item(Item=item, ConditionExpression="attribute_not_exists(sk)")
                    stats["logins_written"] += 1
                except ClientError as exc:
                    if not ts._is_conditional_failure(exc):
                        raise
    finally:
        conn.close()
    return dict(stats)


def recount_messages(touched: dict[tuple[str, str], int]) -> None:
    from boto3.dynamodb.conditions import Key

    for (uid, tid) in touched:
        count = 0
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("task_id").eq(tid),
            "Select": "COUNT",
            "ConsistentRead": True,
        }
        while True:
            resp = ts._messages().query(**kwargs)
            count += resp["Count"]
            if not resp.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        ts._tasks().update_item(
            Key={"user_id": uid, "task_id": tid},
            UpdateExpression="SET message_count = :c",
            ExpressionAttributeValues={":c": count},
            ConditionExpression="attribute_exists(task_id)",
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append", default=[], help="SQLite file or directory (repeatable)")
    ap.add_argument("--s3-bucket", help="Also pull *.db objects from this bucket")
    ap.add_argument("--s3-prefix", default="", help="Key prefix to scan (default: whole bucket)")
    ap.add_argument("--s3-include-versions", action="store_true", help="Include non-current object versions")
    ap.add_argument("--dry-run", action="store_true", help="Read and count only; write nothing")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not args.source and not args.s3_bucket:
        ap.error("give at least one --source or --s3-bucket")

    if not args.dry_run:
        ts.init_db()
        if not ts._endpoint_url():
            logger.info("Writing to AWS tables: %s, %s, %s",
                        ts.tasks_table_name(), ts.messages_table_name(), ts.login_events_table_name())

    with tempfile.TemporaryDirectory(prefix="migrate_s3_") as tmp:
        files = discover_local(args.source)
        if args.s3_bucket:
            files += download_s3(args.s3_bucket, args.s3_prefix, args.s3_include_versions, Path(tmp))

        # Oldest first so the newest copy of a task is written last (and wins ties cleanly).
        files.sort(key=lambda p: p.stat().st_mtime)
        touched: dict[tuple[str, str], int] = {}
        total: dict[str, int] = defaultdict(int)
        for path in files:
            stats = migrate_file(path, dry_run=args.dry_run, touched=touched)
            logger.info("%s -> %s", path, stats)
            for k, v in stats.items():
                total[k] += v

        if not args.dry_run:
            logger.info("Recomputing message_count for %d task(s)...", len(touched))
            recount_messages(touched)

    logger.info("TOTAL (%d file(s)): %s%s", len(files), dict(total), "  [dry-run]" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
