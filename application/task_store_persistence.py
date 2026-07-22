"""Persist tasks.db via S3 Files mount or direct S3 (local) using working-copy pattern."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading

from application import app_data_backend as backend

logger = logging.getLogger("task_store_persistence")

_PERSIST_DEBOUNCE_SECONDS = 20.0

_persist_lock = threading.Lock()
_persist_timer: threading.Timer | None = None
_persist_dirty = False


def mount_dir() -> str:
    return backend.mount_dir()


def persistence_enabled() -> bool:
    """True when durable storage is available (mount or S3)."""
    return backend.backend_mode() in {"mount", "s3"}


def working_db_path() -> str:
    return backend.working_tasks_db_path()


def persistent_db_path() -> str:
    """Mount path for ECS; for S3 mode returns the logical s3:// URI for logs."""
    mode = backend.backend_mode()
    if mode == "s3":
        bucket, _ = backend.s3_bucket_and_region()
        return f"s3://{bucket}/{backend.tasks_db_s3_key()}"
    return backend.persistent_tasks_db_path()


def _db_ready(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _copy_db_files(source: str, destination: str) -> None:
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copy2(source, destination)
    for suffix in ("-wal", "-shm"):
        src = source + suffix
        dst = destination + suffix
        if os.path.isfile(src):
            shutil.copy2(src, dst)
        elif os.path.isfile(dst):
            os.remove(dst)


def _checkpoint_sqlite(db_path: str) -> None:
    if not os.path.isfile(db_path):
        return
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()


def _remove_db_files(path: str) -> None:
    for candidate in (path, path + "-wal", path + "-shm"):
        try:
            if os.path.isfile(candidate):
                os.remove(candidate)
        except OSError as exc:
            logger.warning("Could not remove %s: %s", candidate, exc)


def _restore_from_mount(working: str, persistent: str) -> None:
    os.makedirs(os.path.dirname(working), exist_ok=True)

    if _db_ready(persistent):
        _remove_db_files(working)
        _copy_db_files(persistent, working)
        logger.info("Restored task DB from S3 Files mount: %s -> %s", persistent, working)
        return

    if os.path.isfile(persistent):
        logger.warning(
            "Persistent task DB empty, starting fresh: %s (size=%s)",
            persistent,
            os.path.getsize(persistent),
        )
    else:
        logger.info("No persistent task DB yet at %s; creating fresh working DB", persistent)

    if any(os.path.isfile(working + suffix) for suffix in ("", "-wal", "-shm")):
        logger.info(
            "Removing pre-existing working task DB (e.g. image-baked test data): %s",
            working,
        )
        _remove_db_files(working)


def _restore_from_s3(working: str) -> None:
    bucket, region = backend.s3_bucket_and_region()
    if not bucket:
        return
    key = backend.tasks_db_s3_key()
    os.makedirs(os.path.dirname(working), exist_ok=True)

    tmp = working + ".s3download"
    try:
        if backend.download_s3_file(bucket, key, tmp, region) and _db_ready(tmp):
            _remove_db_files(working)
            os.replace(tmp, working)
            # Drop stale WAL/SHM from a previous local session.
            for suffix in ("-wal", "-shm"):
                side = working + suffix
                if os.path.isfile(side):
                    os.remove(side)
            logger.info("Restored task DB from S3: s3://%s/%s -> %s", bucket, key, working)
            return
        logger.info(
            "No S3 task DB at s3://%s/%s; using local working DB %s",
            bucket,
            key,
            working,
        )
    finally:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def restore_tasks_db() -> None:
    """Prepare working tasks.db from mount or S3 when durable storage is available."""
    working = working_db_path()
    mode = backend.backend_mode()

    if mode == "mount":
        _restore_from_mount(working, backend.persistent_tasks_db_path())
        return

    if mode == "s3":
        _restore_from_s3(working)
        return

    logger.info(
        "Task DB persistence disabled (no mount at %s and no s3_bucket); local only",
        mount_dir(),
    )


def _persist_to_mount(working: str, persistent: str) -> None:
    _checkpoint_sqlite(working)
    _copy_db_files(working, persistent)
    logger.info("Persisted task DB to S3 Files mount: %s -> %s", working, persistent)


def _persist_to_s3(working: str) -> None:
    bucket, region = backend.s3_bucket_and_region()
    if not bucket:
        return
    _checkpoint_sqlite(working)
    # Upload main DB only (WAL checkpointed into it).
    backend.upload_s3_file(bucket, backend.tasks_db_s3_key(), working, region)


def persist_tasks_db(*, force: bool = False) -> None:
    """Flush the working SQLite DB to mount or S3."""
    global _persist_dirty

    mode = backend.backend_mode()
    if mode == "local":
        return

    working = working_db_path()

    with _persist_lock:
        if not force and not _persist_dirty:
            return
        if not _db_ready(working):
            logger.warning("Working task DB missing, skip persist: %s", working)
            _persist_dirty = False
            return

        try:
            if mode == "mount":
                _persist_to_mount(working, backend.persistent_tasks_db_path())
            elif mode == "s3":
                _persist_to_s3(working)
            _persist_dirty = False
        except Exception:
            logger.exception("Failed to persist task DB (%s)", mode)


def schedule_persist() -> None:
    """Debounced persist after task/message mutations."""
    global _persist_timer, _persist_dirty

    if backend.backend_mode() == "local":
        return

    _persist_dirty = True

    def _run() -> None:
        persist_tasks_db(force=True)

    with _persist_lock:
        if _persist_timer is not None:
            _persist_timer.cancel()
        _persist_timer = threading.Timer(_PERSIST_DEBOUNCE_SECONDS, _run)
        _persist_timer.daemon = True
        _persist_timer.start()


def flush_persist() -> None:
    """Cancel pending debounce and persist immediately."""
    global _persist_timer

    with _persist_lock:
        if _persist_timer is not None:
            _persist_timer.cancel()
            _persist_timer = None
    persist_tasks_db(force=True)
