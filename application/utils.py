import logging
import sys
import json
import traceback
import boto3
import os
from contextlib import contextmanager
from urllib import parse

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("utils")

script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, "config.json")
favorite_tools_path = os.path.join(script_dir, "favorite_tools.json")
# ECS: /mnt/app-data (prefix app-data/) for tasks.db, graph, settings.
# Runtime: /mnt/workspace (prefix agentcore-sessions/) for skills/artifacts/checkpoints.
def _default_session_storage_dir() -> str:
    """Prefer ECS app-data mount, then Runtime workspace, then local fallback."""
    for candidate in ("/mnt/app-data", "/mnt/workspace"):
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(script_dir, ".session_storage")


SESSION_STORAGE_DIR = os.environ.get("SESSION_STORAGE_DIR") or _default_session_storage_dir()

# S3 Files FS prefix for Runtime workspace → s3://{bucket}/agentcore-sessions/
S3_FILES_SESSION_PREFIX = "agentcore-sessions"


def sanitize_user_path_segment(user_id: str | None) -> str | None:
    """Return a safe single path segment for per-user workspace folders, or None."""
    if not user_id:
        return None
    # Collapse path separators so user_id cannot escape the intended prefix.
    segment = (
        str(user_id)
        .strip()
        .replace("/", "_")
        .replace("\\", "_")
        .replace("..", "_")
    )
    return segment or None


def get_user_artifacts_dir(user_id: str | None) -> str:
    """Logical path for user artifacts (Runtime /mnt/workspace when present)."""
    segment = sanitize_user_path_segment(user_id) or "default"
    root = "/mnt/workspace" if os.path.isdir("/mnt/workspace") else SESSION_STORAGE_DIR
    return os.path.join(root, segment, "artifacts")


def ensure_user_artifacts_dir(user_id: str | None) -> str:
    """Create artifacts dir under Runtime workspace when available; skip on ECS app-data."""
    artifacts_dir = get_user_artifacts_dir(user_id)
    if not os.path.isdir("/mnt/workspace") and os.path.isdir("/mnt/app-data"):
        return artifacts_dir
    os.makedirs(artifacts_dir, exist_ok=True)
    logger.info("user artifacts dir ready: %s", artifacts_dir)
    return artifacts_dir


def get_user_skills_dir(user_id: str | None) -> str:
    """Logical path for user skills (Runtime /mnt/workspace only).

    Web UI discovers skill-creator skills via S3
    (``agentcore-sessions/{user}/skills/``), not under app-data.
    """
    segment = sanitize_user_path_segment(user_id) or "default"
    root = "/mnt/workspace" if os.path.isdir("/mnt/workspace") else SESSION_STORAGE_DIR
    return os.path.join(root, segment, "skills")


def ensure_user_skills_dir(user_id: str | None) -> str:
    """Create user skills dir under the Runtime workspace mount when available."""
    skills_dir = get_user_skills_dir(user_id)
    # ECS mounts app-data only — do not create a misleading skills/ tree there.
    if not os.path.isdir("/mnt/workspace") and os.path.isdir("/mnt/app-data"):
        return skills_dir
    os.makedirs(skills_dir, exist_ok=True)
    logger.info("user skills dir ready: %s", skills_dir)
    return skills_dir


def get_user_graph_dir(user_id: str | None) -> str:
    """Absolute path to {SESSION_STORAGE_DIR}/{user_id}/graph (does not create)."""
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        segment = "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "graph")


def ensure_user_graph_dir(user_id: str | None) -> str:
    """Create session graph workspace: corpus/ + out/ (shared extract+publish).

    Returns the graph root: {SESSION_STORAGE_DIR}/{user_id}/graph
    """
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        raise ValueError(
            "Invalid user_id for graph path; expected a plain user id, "
            "not a signed session cookie"
        )
    graph_dir = os.path.join(SESSION_STORAGE_DIR, segment, "graph")
    for name in ("corpus", "out"):
        os.makedirs(os.path.join(graph_dir, name), exist_ok=True)
    logger.info("user graph dir ready: %s", graph_dir)
    return graph_dir


def user_graph_html_path(user_id: str | None) -> str:
    """Published HTML: {SESSION_STORAGE_DIR}/{user_id}/graph/out/graph.html"""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "graph", "out", "graph.html")

def get_user_wiki_dir(user_id: str | None) -> str:
    """Per-user wiki root: ``{SESSION_STORAGE_DIR}/{user_id}/wiki``.

    Replaces the old global ``AGENT_WIKI_DIR`` (~/Documents/wiki) so each
    login user gets an isolated raw/ + graphify-out/ tree.
    """
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        segment = "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "wiki")


def get_wiki_dir(user_id: str | None = None) -> str:
    """Alias for :func:`get_user_wiki_dir` (requires ``user_id`` in multi-user use)."""
    return get_user_wiki_dir(user_id)


def ensure_user_wiki_dir(user_id: str | None) -> str:
    """Create ``{user}/wiki``, ``raw/``, ``graphify-out/`` and return wiki root."""
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        raise ValueError(
            "Invalid user_id for wiki path; expected a plain user id, "
            "not a signed session cookie"
        )
    wiki = os.path.join(SESSION_STORAGE_DIR, segment, "wiki")
    for name in ("", "raw", "graphify-out", os.path.join("graphify-out", "converted")):
        os.makedirs(os.path.join(wiki, name) if name else wiki, exist_ok=True)
    logger.info("user wiki dir ready: %s", wiki)
    return wiki


def ensure_wiki_dir(user_id: str | None = None) -> str:
    """Alias for :func:`ensure_user_wiki_dir`."""
    return ensure_user_wiki_dir(user_id)


def wiki_graphify_out_dir(user_id: str | None = None) -> str:
    """``{SESSION_STORAGE}/{user}/wiki/graphify-out``."""
    return os.path.join(get_user_wiki_dir(user_id), "graphify-out")


def wiki_graph_html_path(user_id: str | None = None) -> str:
    """Pattern UI HTML served by /api/wiki/graph (Force Atlas / Neo4j / Holistic)."""
    return os.path.join(wiki_graphify_out_dir(user_id), "app-graph.html")


def wiki_graph_json_path(user_id: str | None = None) -> str:
    return os.path.join(wiki_graphify_out_dir(user_id), "graph.json")


def wiki_graph_pattern_path(user_id: str | None = None) -> str:
    return os.path.join(wiki_graphify_out_dir(user_id), ".wiki_graph_pattern")


def get_wiki_graph_pattern(user_id: str | None = None) -> str:
    """Selected Wiki Graph HTML pattern (pattern1|2|3)."""
    path = wiki_graph_pattern_path(user_id)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            if raw:
                return normalize_graph_pattern(raw)
        except OSError:
            pass
    return DEFAULT_GRAPH_PATTERN


def set_wiki_graph_pattern(
    pattern: object | None, user_id: str | None = None
) -> str:
    """Persist Wiki Graph pattern under the user's graphify-out."""
    pid = normalize_graph_pattern(pattern)
    out = wiki_graphify_out_dir(user_id)
    os.makedirs(out, exist_ok=True)
    with open(wiki_graph_pattern_path(user_id), "w", encoding="utf-8") as f:
        f.write(pid + "\n")
    return pid


MAX_WIKI_SOURCE_FOLDERS = 3


def wiki_sources_path(user_id: str | None = None) -> str:
    """Per-user sources file: ``{SESSION_STORAGE}/{user}/wiki/wiki_sources.json``."""
    return os.path.join(get_user_wiki_dir(user_id), "wiki_sources.json")


def _normalize_wiki_source_path(value: object | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return os.path.abspath(os.path.expanduser(raw))


def _normalize_wiki_source_url(value: object | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    lower = raw.lower()
    if not (lower.startswith("http://") or lower.startswith("https://")):
        raise ValueError(f"URL은 http:// 또는 https:// 로 시작해야 합니다: {raw}")
    return raw


def _default_wiki_sources_doc() -> dict[str, list[str]]:
    return {
        "AGENT_WIKI_SOURCES": [],
        "AGENT_WIKI_URLS": [],
        "AGENT_WIKI_FILES": [],
    }


def _read_wiki_sources_file(path: str) -> dict[str, list[str]] | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return None
        doc = _default_wiki_sources_doc()
        folders = raw.get("AGENT_WIKI_SOURCES")
        urls = raw.get("AGENT_WIKI_URLS")
        files = raw.get("AGENT_WIKI_FILES")
        if isinstance(folders, list):
            doc["AGENT_WIKI_SOURCES"] = [str(x) for x in folders]
        if isinstance(urls, list):
            doc["AGENT_WIKI_URLS"] = [str(x) for x in urls]
        if isinstance(files, list):
            doc["AGENT_WIKI_FILES"] = [str(x) for x in files]
        return doc
    except Exception as e:
        logger.warning("Failed to load wiki sources %s: %s", path, e)
        return None


def load_wiki_sources(user_id: str | None = None) -> dict[str, list[str]]:
    """Load Wiki Sync folders/URLs/files from ``{user}/wiki/wiki_sources.json``."""
    path = wiki_sources_path(user_id)
    doc = _read_wiki_sources_file(path)
    if doc is not None:
        return doc
    return _default_wiki_sources_doc()


def _write_wiki_sources_doc(
    doc: dict[str, list[str]], *, user_id: str | None = None
) -> None:
    ensure_user_wiki_dir(user_id)
    path = wiki_sources_path(user_id)
    payload = {
        "AGENT_WIKI_SOURCES": list(doc.get("AGENT_WIKI_SOURCES") or []),
        "AGENT_WIKI_URLS": list(doc.get("AGENT_WIKI_URLS") or []),
        "AGENT_WIKI_FILES": list(doc.get("AGENT_WIKI_FILES") or []),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def get_wiki_source_folders(user_id: str | None = None) -> list[str]:
    """Configured Wiki Sync source folders (max 3) for the user.

    Empty list → Sync falls back to ``{wiki}/raw`` if present, else wiki root.
    """
    raw = load_wiki_sources(user_id).get("AGENT_WIKI_SOURCES") or []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        path = _normalize_wiki_source_path(item)
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(path)
        if len(out) >= MAX_WIKI_SOURCE_FOLDERS:
            break
    return out


def get_wiki_source_urls(user_id: str | None = None) -> list[str]:
    """Append-only URL ingest history for the user (audit trail)."""
    raw = load_wiki_sources(user_id).get("AGENT_WIKI_URLS") or []
    out: list[str] = []
    for item in raw:
        try:
            url = _normalize_wiki_source_url(item)
        except ValueError:
            text = str(item or "").strip()
            if text:
                out.append(text)
            continue
        if url:
            out.append(url)
    return out


def get_wiki_source_files(user_id: str | None = None) -> list[str]:
    """Append-only uploaded document paths under ``{wiki}/raw``."""
    raw = load_wiki_sources(user_id).get("AGENT_WIKI_FILES") or []
    out: list[str] = []
    for item in raw:
        path = _normalize_wiki_source_path(item)
        if path:
            out.append(path)
    return out


def append_wiki_source_files(
    paths: list[str], *, user_id: str | None = None
) -> list[str]:
    """Append saved raw document paths to wiki_sources.json (dedupe by path)."""
    doc = load_wiki_sources(user_id)
    history = list(doc.get("AGENT_WIKI_FILES") or [])
    seen = {os.path.abspath(os.path.expanduser(p)) for p in history if p}
    added = 0
    for item in paths:
        path = _normalize_wiki_source_path(item)
        if not path or path in seen:
            continue
        history.append(path)
        seen.add(path)
        added += 1
    _write_wiki_sources_doc(
        {
            "AGENT_WIKI_SOURCES": list(doc.get("AGENT_WIKI_SOURCES") or []),
            "AGENT_WIKI_URLS": list(doc.get("AGENT_WIKI_URLS") or []),
            "AGENT_WIKI_FILES": history,
        },
        user_id=user_id,
    )
    if added:
        logger.info(
            "wiki sources appended files user=%s count=%s",
            sanitize_user_path_segment(user_id) or "default",
            added,
        )
    return history


def set_wiki_source_folders(
    folders: list[object] | None, user_id: str | None = None
) -> list[str]:
    """Persist up to 3 Wiki Sync source folders for the user."""
    return set_wiki_sources(folders=folders, user_id=user_id)["folders"]


def browse_wiki_source_dirs(
    path: object | None = None, *, user_id: str | None = None
) -> dict[str, object]:
    """List child directories for the Wiki Configure source picker."""
    home = os.path.abspath(os.path.expanduser("~"))
    documents = os.path.join(home, "Documents")
    wiki = get_user_wiki_dir(user_id)

    raw = str(path or "").strip()
    if raw:
        target = _normalize_wiki_source_path(raw)
    elif os.path.isdir(documents):
        target = documents
    else:
        target = home
    if not target or not os.path.isdir(target):
        raise ValueError(f"폴더가 없습니다: {raw or target}")

    parent = os.path.dirname(target)
    if parent == target:
        parent = None

    entries: list[dict[str, str]] = []
    try:
        names = sorted(os.listdir(target), key=str.lower)
    except OSError as exc:
        raise ValueError(f"폴더를 읽을 수 없습니다: {target}") from exc

    for name in names:
        if name.startswith("."):
            continue
        child = os.path.join(target, name)
        if not os.path.isdir(child):
            continue
        entries.append({"name": name, "path": child})

    shortcuts: list[dict[str, str]] = []
    for name, candidate in (
        ("Home", home),
        ("Documents", documents),
        ("Wiki", wiki),
        ("Wiki raw", os.path.join(wiki, "raw")),
    ):
        if os.path.isdir(candidate):
            shortcuts.append({"name": name, "path": candidate})

    return {
        "path": target,
        "parent": parent,
        "dirs": entries,
        "shortcuts": shortcuts,
    }


def append_wiki_source_url(
    url: str, *, user_id: str | None = None
) -> list[str]:
    """Append a URL to the user's ingest history."""
    normalized = _normalize_wiki_source_url(url)
    if not normalized:
        raise ValueError("URL이 비어 있습니다.")
    doc = load_wiki_sources(user_id)
    history = list(doc.get("AGENT_WIKI_URLS") or [])
    history.append(normalized)
    folders = list(doc.get("AGENT_WIKI_SOURCES") or [])
    files = list(doc.get("AGENT_WIKI_FILES") or [])
    _write_wiki_sources_doc(
        {
            "AGENT_WIKI_SOURCES": folders,
            "AGENT_WIKI_URLS": history,
            "AGENT_WIKI_FILES": files,
        },
        user_id=user_id,
    )
    logger.info(
        "wiki sources appended URL user=%s history=%s",
        sanitize_user_path_segment(user_id) or "default",
        normalized,
    )
    return history


def ingest_wiki_url(url: str, *, user_id: str | None = None) -> dict[str, object]:
    """Fetch a URL into the user's ``{wiki}/raw`` and append URL history."""
    from pathlib import Path

    from graphify.ingest import ingest

    normalized = _normalize_wiki_source_url(url)
    if not normalized:
        raise ValueError("URL이 비어 있습니다.")
    wiki = Path(ensure_user_wiki_dir(user_id))
    raw_dir = wiki / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = ingest(normalized, raw_dir)
    history = append_wiki_source_url(normalized, user_id=user_id)
    return {"url": normalized, "path": str(path), "urls": history}


def _wiki_raw_dest_path(raw_dir: "Path", filename: str) -> "Path":
    """Sanitize upload name under ``raw/``. Same name → overwrite."""
    from pathlib import Path

    raw_dir = Path(raw_dir)
    name = Path(str(filename or "").strip() or "upload.bin").name
    # Block path traversal in uploaded names.
    name = name.replace("\x00", "").replace("/", "_").replace("\\", "_")
    if not name or name in (".", ".."):
        name = "upload.bin"
    return raw_dir / name


def save_wiki_raw_uploads(
    files: list[tuple[str, bytes]],
    *,
    user_id: str | None = None,
) -> dict[str, object]:
    """Write uploaded files into ``{user}/wiki/raw`` (overwrite same filename)."""
    from pathlib import Path

    if not files:
        raise ValueError("업로드할 파일이 없습니다.")

    wiki = Path(ensure_user_wiki_dir(user_id))
    raw_dir = wiki / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict[str, object]] = []
    for filename, data in files:
        if data is None:
            continue
        dest = _wiki_raw_dest_path(raw_dir, filename)
        overwritten = dest.is_file()
        dest.write_bytes(data)
        saved.append(
            {
                "name": dest.name,
                "path": str(dest),
                "bytes": len(data),
                "overwritten": overwritten,
            }
        )
        logger.info(
            "wiki raw upload user=%s → %s (%s bytes%s)",
            sanitize_user_path_segment(user_id) or "default",
            dest,
            len(data),
            ", overwrite" if overwritten else "",
        )

    if not saved:
        raise ValueError("저장할 파일이 없습니다.")

    file_history = append_wiki_source_files(
        [str(item["path"]) for item in saved],
        user_id=user_id,
    )

    return {
        "wiki_dir": str(wiki),
        "raw_dir": str(raw_dir),
        "saved": saved,
        "count": len(saved),
        "files": file_history,
    }


def set_wiki_sources(
    *,
    folders: list[object] | None = None,
    user_id: str | None = None,
) -> dict[str, list[str]]:
    """Persist Wiki Sync folders for the user (URL/file history preserved)."""
    doc = load_wiki_sources(user_id)
    url_history = list(doc.get("AGENT_WIKI_URLS") or [])
    file_history = list(doc.get("AGENT_WIKI_FILES") or [])

    if folders is None:
        cleaned_folders = get_wiki_source_folders(user_id)
    else:
        cleaned_folders = []
        seen_f: set[str] = set()
        for item in folders or []:
            path = _normalize_wiki_source_path(item)
            if not path or path in seen_f:
                continue
            if not os.path.isdir(path):
                raise ValueError(f"폴더가 없습니다: {path}")
            seen_f.add(path)
            cleaned_folders.append(path)
            if len(cleaned_folders) >= MAX_WIKI_SOURCE_FOLDERS:
                break

    _write_wiki_sources_doc(
        {
            "AGENT_WIKI_SOURCES": cleaned_folders,
            "AGENT_WIKI_URLS": url_history,
            "AGENT_WIKI_FILES": file_history,
        },
        user_id=user_id,
    )
    logger.info(
        "wiki sources saved user=%s folders=%s url_history=%s files=%s",
        sanitize_user_path_segment(user_id) or "default",
        cleaned_folders,
        len(url_history),
        len(file_history),
    )
    return {
        "folders": cleaned_folders,
        "urls": get_wiki_source_urls(user_id),
        "files": get_wiki_source_files(user_id),
    }



# Extract caches are not needed for Runtime recall_graph_memory.
_GRAPH_MIRROR_SKIP_DIR_NAMES = frozenset({"cache", "graphify-out"})
_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


@contextmanager
def _without_env_proxies():
    """Drop HTTP(S)_PROXY for the block (Cursor agent proxies break local boto3)."""
    saved = {key: os.environ.pop(key, None) for key in _PROXY_ENV_KEYS}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def sync_user_graph_to_runtime_storage(user_id: str | None) -> dict[str, int]:
    """Mirror ECS/local graph → S3 agentcore-sessions for AgentCore Runtime.

    Knowledge graphs live on app-data (``SESSION_STORAGE_DIR`` / ``app-data/``).
    AgentCore Runtime only mounts ``agentcore-sessions/`` at ``/mnt/workspace``,
    so ``recall_graph_memory`` cannot see app-data. After each successful
    pipeline/publish, upload ``{user}/graph/`` to
    ``s3://{bucket}/agentcore-sessions/{user}/graph/`` so Runtime can read
    ``/mnt/workspace/{user}/graph/out/graph.json``.

    Returns counts: ``{"uploaded": N, "deleted": M}``. Missing graph or S3
    config → empty counts (logged, not raised).
    """
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        return {"uploaded": 0, "deleted": 0}

    graph_root = get_user_graph_dir(user_id)
    graph_json = os.path.join(graph_root, "out", "graph.json")
    if not os.path.isfile(graph_json):
        logger.info(
            "skip graph→runtime mirror: no graph.json for %s at %s",
            segment,
            graph_json,
        )
        return {"uploaded": 0, "deleted": 0}

    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    bucket = (cfg.get("s3_bucket") if isinstance(cfg, dict) else None) or s3_bucket
    region = (cfg.get("region") if isinstance(cfg, dict) else None) or bedrock_region
    if not bucket:
        logger.warning("skip graph→runtime mirror: s3_bucket not configured")
        return {"uploaded": 0, "deleted": 0}

    dest_prefix = f"{S3_FILES_SESSION_PREFIX}/{segment}/graph/"
    local_files: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(graph_root):
        dirnames[:] = [d for d in dirnames if d not in _GRAPH_MIRROR_SKIP_DIR_NAMES]
        for name in filenames:
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, graph_root).replace(os.sep, "/")
            local_files[rel] = abs_path

    if not local_files:
        return {"uploaded": 0, "deleted": 0}

    uploaded = 0
    failed = 0
    deleted = 0
    # Local uvicorn often inherits Cursor's ephemeral HTTP(S)_PROXY
    # (127.0.0.1:61xxx). That proxy dies with the agent session and breaks
    # every boto3 upload — clear env proxies for this sync only.
    with _without_env_proxies():
        s3 = boto3.client("s3", region_name=region)
        for rel, abs_path in sorted(local_files.items()):
            key = f"{dest_prefix}{rel}"
            try:
                s3.upload_file(abs_path, bucket, key)
                uploaded += 1
            except Exception as e:
                failed += 1
                logger.warning("graph mirror upload failed %s: %s", key, e)
        if failed:
            logger.warning(
                "graph→runtime mirror incomplete user=%s uploaded=%s failed=%s",
                segment,
                uploaded,
                failed,
            )

        try:
            paginator = s3.get_paginator("list_objects_v2")
            remote_keys: list[str] = []
            for page in paginator.paginate(Bucket=bucket, Prefix=dest_prefix):
                for obj in page.get("Contents") or []:
                    key = obj.get("Key") or ""
                    if key and not key.endswith("/"):
                        remote_keys.append(key)
            keep = {f"{dest_prefix}{rel}" for rel in local_files}
            stale = [key for key in remote_keys if key not in keep]
            for key in stale:
                try:
                    s3.delete_object(Bucket=bucket, Key=key)
                    deleted += 1
                except Exception as e:
                    logger.warning("graph mirror delete failed %s: %s", key, e)
        except Exception as e:
            logger.warning("graph mirror list/delete skipped for %s: %s", segment, e)

    logger.info(
        "Mirrored graph → runtime storage user=%s uploaded=%s deleted=%s prefix=s3://%s/%s",
        segment,
        uploaded,
        deleted,
        bucket,
        dest_prefix,
    )
    return {"uploaded": uploaded, "deleted": deleted}


_WIKI_MIRROR_SKIP_DIR_NAMES = frozenset({"cache"})


def sync_user_wiki_to_runtime_storage(user_id: str | None) -> dict[str, int]:
    """Mirror local wiki → S3 agentcore-sessions for AgentCore Runtime ``recall_wiki``.

    Uploads ``{user}/wiki/`` (including ``graphify-out/``) to
    ``s3://{bucket}/agentcore-sessions/{user}/wiki/``.
    """
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        return {"uploaded": 0, "deleted": 0}

    wiki_root = get_user_wiki_dir(user_id)
    graph_json = wiki_graph_json_path(user_id)
    if not os.path.isfile(graph_json):
        logger.info(
            "skip wiki→runtime mirror: no graph.json for %s at %s",
            segment,
            graph_json,
        )
        return {"uploaded": 0, "deleted": 0}

    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    bucket = (cfg.get("s3_bucket") if isinstance(cfg, dict) else None) or s3_bucket
    region = (cfg.get("region") if isinstance(cfg, dict) else None) or bedrock_region
    if not bucket:
        logger.warning("skip wiki→runtime mirror: s3_bucket not configured")
        return {"uploaded": 0, "deleted": 0}

    dest_prefix = f"{S3_FILES_SESSION_PREFIX}/{segment}/wiki/"
    local_files: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(wiki_root):
        dirnames[:] = [d for d in dirnames if d not in _WIKI_MIRROR_SKIP_DIR_NAMES]
        for name in filenames:
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, wiki_root).replace(os.sep, "/")
            local_files[rel] = abs_path

    if not local_files:
        return {"uploaded": 0, "deleted": 0}

    uploaded = 0
    failed = 0
    deleted = 0
    with _without_env_proxies():
        s3 = boto3.client("s3", region_name=region)
        for rel, abs_path in sorted(local_files.items()):
            key = f"{dest_prefix}{rel}"
            try:
                s3.upload_file(abs_path, bucket, key)
                uploaded += 1
            except Exception as e:
                failed += 1
                logger.warning("wiki mirror upload failed %s: %s", key, e)
        if failed:
            logger.warning(
                "wiki→runtime mirror incomplete user=%s uploaded=%s failed=%s",
                segment,
                uploaded,
                failed,
            )

        try:
            paginator = s3.get_paginator("list_objects_v2")
            remote_keys: list[str] = []
            for page in paginator.paginate(Bucket=bucket, Prefix=dest_prefix):
                for obj in page.get("Contents") or []:
                    key = obj.get("Key") or ""
                    if key and not key.endswith("/"):
                        remote_keys.append(key)
            keep = {f"{dest_prefix}{rel}" for rel in local_files}
            stale = [key for key in remote_keys if key not in keep]
            for key in stale:
                try:
                    s3.delete_object(Bucket=bucket, Key=key)
                    deleted += 1
                except Exception as e:
                    logger.warning("wiki mirror delete failed %s: %s", key, e)
        except Exception as e:
            logger.warning("wiki mirror list/delete skipped for %s: %s", segment, e)

    logger.info(
        "Mirrored wiki → runtime storage user=%s uploaded=%s deleted=%s prefix=s3://%s/%s",
        segment,
        uploaded,
        deleted,
        bucket,
        dest_prefix,
    )
    return {"uploaded": uploaded, "deleted": deleted}


GRAPH_PATTERNS = ("pattern1", "pattern2", "pattern3")
DEFAULT_GRAPH_PATTERN = "pattern1"

_DEFAULT_USER_SETTINGS: dict[str, object] = {
    "knowledge_graph_enabled": True,
    "graph_pattern": DEFAULT_GRAPH_PATTERN,
    "foundation_model_parser_enabled": False,
}


def normalize_graph_pattern(value: object | None) -> str:
    raw = str(value or "").strip().lower().replace(" ", "").replace("_", "")
    aliases = {
        "pattern1": "pattern1",
        "p1": "pattern1",
        "1": "pattern1",
        "forceatlas": "pattern1",
        "pattern2": "pattern2",
        "p2": "pattern2",
        "2": "pattern2",
        "neo4j": "pattern2",
        "neo4jexplore": "pattern2",
        "pattern3": "pattern3",
        "p3": "pattern3",
        "3": "pattern3",
        "holistic": "pattern3",
        "holisticview": "pattern3",
    }
    return aliases.get(raw, DEFAULT_GRAPH_PATTERN)


def get_user_db_path(user_id: str | None) -> str:
    """Durable per-user tasks/messages DB: {SESSION_STORAGE_DIR}/{user_id}/{user_id}.db."""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, f"{segment}.db")


def get_user_settings_path(user_id: str | None) -> str:
    """Absolute path to {SESSION_STORAGE_DIR}/{user_id}/settings.json (does not create)."""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "settings.json")


def load_user_settings(user_id: str | None) -> dict[str, object]:
    """Load per-user UI/feature settings. Missing file → defaults (KG on)."""
    settings = dict(_DEFAULT_USER_SETTINGS)
    path = get_user_settings_path(user_id)
    if not os.path.isfile(path):
        return settings
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            if "knowledge_graph_enabled" in raw:
                settings["knowledge_graph_enabled"] = bool(raw["knowledge_graph_enabled"])
            if "graph_pattern" in raw:
                settings["graph_pattern"] = normalize_graph_pattern(raw.get("graph_pattern"))
            if "foundation_model_parser_enabled" in raw:
                settings["foundation_model_parser_enabled"] = bool(
                    raw["foundation_model_parser_enabled"]
                )
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to load user settings %s: %s", path, e)
    return settings


def save_user_settings(user_id: str | None, **updates: object) -> dict[str, object]:
    """Merge updates into per-user settings.json and return the full settings."""
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        raise ValueError(
            "Invalid user_id for settings path; expected a plain user id, "
            "not a signed session cookie"
        )
    user_dir = os.path.join(SESSION_STORAGE_DIR, segment)
    os.makedirs(user_dir, exist_ok=True)
    settings = load_user_settings(user_id)
    for key, value in updates.items():
        if key == "knowledge_graph_enabled":
            settings[key] = bool(value)
        elif key == "graph_pattern":
            settings[key] = normalize_graph_pattern(value)
        elif key == "foundation_model_parser_enabled":
            settings[key] = bool(value)
    path = get_user_settings_path(user_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    logger.info("user settings saved: %s -> %s", path, settings)
    return settings


def is_knowledge_graph_enabled(user_id: str | None) -> bool:
    """True when Knowledge Graph feature is on (default)."""
    return bool(load_user_settings(user_id).get("knowledge_graph_enabled", True))


def is_foundation_model_parser_enabled(user_id: str | None) -> bool:
    """True when Wiki Sync uses multimodal PDF→images→LLM (default: False)."""
    return bool(
        load_user_settings(user_id).get("foundation_model_parser_enabled", False)
    )


def set_foundation_model_parser_enabled(
    enabled: bool, *, user_id: str | None
) -> bool:
    """Persist Foundation Model Parser toggle; returns the stored value."""
    settings = save_user_settings(
        user_id, foundation_model_parser_enabled=bool(enabled)
    )
    return bool(settings.get("foundation_model_parser_enabled", False))





def is_hybrid_graph_search_enabled() -> bool:
    """True when config.json hybrid_graph_search is enable (embedding vector search)."""
    cfg = load_config() or {}
    raw = str(cfg.get("hybrid_graph_search") or "").strip().lower()
    return raw in {"enable", "enabled", "on", "true", "1", "yes"}


def get_graph_pattern(user_id: str | None) -> str:
    """Selected Knowledge Graph HTML pattern (pattern1|pattern2|pattern3)."""
    return normalize_graph_pattern(
        load_user_settings(user_id).get("graph_pattern", DEFAULT_GRAPH_PATTERN)
    )



def get_user_skills_list_path(user_id: str | None) -> str:
    """Absolute path to {SESSION_STORAGE_DIR}/{user_id}/skills.list (does not create)."""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "skills.list")


def _list_skill_dir_names(skills_dir: str) -> list[str]:
    """Return subdirectory names that contain SKILL.md."""
    if not os.path.isdir(skills_dir):
        return []
    names: list[str] = []
    try:
        entries = sorted(os.listdir(skills_dir))
    except OSError as e:
        logger.warning("Failed to list skills directory %s: %s", skills_dir, e)
        return []
    for entry in entries:
        if os.path.isfile(os.path.join(skills_dir, entry, "SKILL.md")):
            names.append(entry)
    return names


def _list_user_skill_names_from_s3(user_id: str | None) -> list[str]:
    """List skill-creator skill dirs under s3://{bucket}/agentcore-sessions/{user}/skills/.

    ECS mounts app-data only; user skills always come from this S3 prefix.
    Only directories that contain SKILL.md are included.
    """
    if not user_id:
        return []
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        return []
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    bucket = (cfg.get("s3_bucket") if isinstance(cfg, dict) else None) or globals().get(
        "s3_bucket"
    )
    region = (cfg.get("region") if isinstance(cfg, dict) else None) or globals().get(
        "bedrock_region", "us-west-2"
    )
    if not bucket:
        # Fall back to local workspace mount when present (local/runtime).
        return _list_skill_dir_names(get_user_skills_dir(user_id))

    prefix = f"{S3_FILES_SESSION_PREFIX}/{segment}/skills/"
    try:
        s3 = boto3.client("s3", region_name=region)
        paginator = s3.get_paginator("list_objects_v2")
        names: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
            for entry in page.get("CommonPrefixes") or []:
                child = (entry.get("Prefix") or "").rstrip("/")
                name = child.rsplit("/", 1)[-1] if child else ""
                if name:
                    names.append(name)

        confirmed: list[str] = []
        for name in sorted(names):
            key = f"{prefix}{name}/SKILL.md"
            try:
                s3.head_object(Bucket=bucket, Key=key)
            except Exception:
                continue
            confirmed.append(name)
            logger.info("Skill discovered (s3): %s", name)
        return confirmed
    except Exception as e:
        logger.warning("Failed to list user skills from S3 for %s: %s", user_id, e)
        return _list_skill_dir_names(get_user_skills_dir(user_id))


def _load_skills_list_file(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
    except FileNotFoundError:
        return []
    except OSError as e:
        logger.warning("Failed to read skills.list %s: %s", path, e)
        return []


def _seed_skill_names(user_id: str | None) -> list[str]:
    """Builtin application/skills.list + skill-creator skills from S3 session prefix."""
    default_path = os.path.join(script_dir, "skills.list")
    builtin = _load_skills_list_file(default_path)
    user_skills = _list_user_skill_names_from_s3(user_id)
    merged: list[str] = []
    seen: set[str] = set()
    for name in builtin + user_skills:
        if name not in seen:
            merged.append(name)
            seen.add(name)
    return merged


def write_user_skills_list(user_id: str | None, names: list[str] | None = None) -> str:
    """Write {SESSION_STORAGE_DIR}/{user_id}/skills.list and return its path."""
    path = get_user_skills_list_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    merged = names if names is not None else _seed_skill_names(user_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(merged) + ("\n" if merged else ""))
    logger.info(
        "wrote user skills.list (%d skills) -> %s",
        len(merged),
        path,
    )
    return path


def update_user_skills_list(user_id: str | None) -> str:
    """Rewrite per-user skills.list from application/skills.list + S3 user skills."""
    return write_user_skills_list(user_id)


def ensure_user_skills_list(user_id: str | None) -> str:
    """Sync skills.list to builtins + S3 agentcore-sessions/{user}/skills/.

    ECS mounts app-data only; user-created skills are listed via S3 API, not the
    local mount. Builtin names come from ``application/skills.list``.
    """
    path = get_user_skills_list_path(user_id)
    desired = _seed_skill_names(user_id)
    existing = _load_skills_list_file(path) if os.path.isfile(path) else []
    if existing == desired:
        logger.info(
            "user skills.list up to date (%d skills) -> %s",
            len(existing),
            path,
        )
        return path
    return write_user_skills_list(user_id, desired)


def _account_id_from_config(config: dict) -> str | None:
    for value in config.values():
        if isinstance(value, str) and value.startswith("arn:aws:"):
            parts = value.split(":")
            if len(parts) > 4 and parts[4].isdigit():
                return parts[4]
    account_id = config.get("accountId")
    return account_id if isinstance(account_id, str) and account_id else None

def _fill_missing_config_defaults(config: dict) -> dict:
    if not config.get("projectName"):
        config["projectName"] = "agentcore"

    if not config.get("region"):
        gateway_region = config.get("agentcore_websearch_gateway_region")
        config["region"] = gateway_region if isinstance(gateway_region, str) and gateway_region else "us-west-2"

    if not config.get("accountId"):
        account_id = _account_id_from_config(config)
        if account_id:
            config["accountId"] = account_id
        else:
            try:
                session = boto3.Session()
                if not config.get("region"):
                    config["region"] = session.region_name or config["region"]
                sts = boto3.client("sts", region_name=config["region"])
                config["accountId"] = sts.get_caller_identity()["Account"]
            except Exception as e:
                logger.warning("Could not resolve accountId from AWS: %s", e)
                config.setdefault("accountId", "000000000000")
    return config

def load_config():
    config: dict = {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("config.json must contain a JSON object")
        config = _fill_missing_config_defaults(loaded)
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        config = _fill_missing_config_defaults({})
    return config


def load_favorite_tools() -> dict[str, list[str]]:
    fallback = {"MCP": [], "SKILL": []}
    try:
        with open(favorite_tools_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.warning("favorite_tools.json not found: %s", favorite_tools_path)
        return fallback
    except Exception as e:
        logger.warning("Failed to load favorite_tools.json: %s", e)
        return fallback

    if not isinstance(data, dict):
        return fallback

    favorites: dict[str, list[str]] = {}
    for key in ("MCP", "SKILL"):
        values = data.get(key, [])
        if isinstance(values, list):
            favorites[key] = [v for v in values if isinstance(v, str) and v.strip()]
        else:
            favorites[key] = []
    return favorites


def get_initial_tool_defaults() -> tuple[list[str], list[str]]:
    favorite_tools = load_favorite_tools()
    default_skills = favorite_tools.get("SKILL") or []
    default_mcp_servers = favorite_tools.get("MCP") or []
    return default_skills, default_mcp_servers

config = load_config()

bedrock_region = config['region']
projectName = config['projectName']
accountId = config['accountId']
s3_bucket = config.get('s3_bucket')
s3_prefix = "docs"
s3_image_prefix = "images"
sharing_url = config.get('sharing_url', '')
knowledge_base_id = config.get('knowledge_base_id')
data_source_id = config.get('data_source_id')


def persist_config_updates(updates):
    """Merge values into in-memory config and write application/config.json."""
    global config
    if not updates:
        return
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        s = value.strip() if isinstance(value, str) else str(value)
        if not s:
            continue
        if config.get(key) != s:
            config[key] = s
            changed = True
    if not changed:
        return
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.write("\n")
        logger.info(
            "Saved config.json updates: %s",
            ", ".join(str(k) for k in updates if updates.get(k)),
        )
    except Exception as e:
        logger.warning("Failed to write config.json: %s", e)


def get_llm_gateway_key() -> str:
    """Resolve LLM Gateway key: ECS-injected env first, then config.json."""
    env_key = (os.environ.get("LLM_GATEWAY_KEY") or "").strip()
    if env_key:
        return env_key
    return (load_config().get("llm_gateway_key") or "").strip()


def sync_llm_gateway_key_secret(key: str) -> None:
    """Best-effort upsert of `{project}/llm-gateway-key` for ECS secret injection."""
    value = (key or "").strip()
    if not value:
        return
    os.environ["LLM_GATEWAY_KEY"] = value
    try:
        from botocore.exceptions import ClientError

        cfg = load_config()
        project = (cfg.get("projectName") or "agentic-work").strip() or "agentic-work"
        region_name = (
            (cfg.get("region") or "").strip()
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-west-2"
        )
        secret_name = f"{project}/llm-gateway-key"
        client = boto3.client("secretsmanager", region_name=region_name)
        try:
            client.put_secret_value(SecretId=secret_name, SecretString=value)
            logger.info("Updated Secrets Manager secret: %s", secret_name)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
            client.create_secret(
                Name=secret_name,
                Description=f"LLM Gateway fallback API key for {project}",
                SecretString=value,
            )
            logger.info("Created Secrets Manager secret: %s", secret_name)
    except Exception as e:
        logger.debug("Could not sync LLM gateway key to Secrets Manager: %s", e)


def get_contents_type(file_name: str) -> str:
    lower = file_name.lower()
    if lower.endswith((".jpg", ".jpeg")):
        content_type = "image/jpeg"
    elif lower.endswith(".png"):
        content_type = "image/png"
    elif lower.endswith(".webp"):
        content_type = "image/webp"
    elif lower.endswith(".gif"):
        content_type = "image/gif"
    elif lower.endswith(".pdf"):
        content_type = "application/pdf"
    elif lower.endswith(".txt"):
        content_type = "text/plain"
    elif lower.endswith(".csv"):
        content_type = "text/csv"
    elif lower.endswith((".ppt", ".pptx")):
        content_type = "application/vnd.ms-powerpoint"
    elif lower.endswith((".doc", ".docx")):
        content_type = "application/msword"
    elif lower.endswith(".xls"):
        content_type = "application/vnd.ms-excel"
    elif lower.endswith(".py"):
        content_type = "text/x-python"
    elif lower.endswith(".js"):
        content_type = "application/javascript"
    elif lower.endswith(".md"):
        content_type = "text/markdown"
    elif lower.endswith((".html", ".htm")):
        content_type = "text/html; charset=utf-8"
    else:
        content_type = "no info"
    return content_type


def _sanitize_s3_user_segment(user_id: str | None) -> str | None:
    """Return a safe single path segment for per-user S3 folders, or None."""
    return sanitize_user_path_segment(user_id)


def upload_to_s3(
    file_bytes: bytes,
    file_name: str,
    user_id: str | None = None,
) -> dict | None:
    """Upload a file to S3 under docs/ (or images/) and return upload metadata.

    When ``user_id`` is provided, the object key becomes
    ``{prefix}/{user_id}/{file_name}`` so each user has a separate folder.
    """
    if not s3_bucket:
        logger.error("s3_bucket is not configured")
        return None

    try:
        s3_client = boto3.client(service_name="s3", region_name=bedrock_region)
        content_type = get_contents_type(file_name)
        logger.info("content_type: %s", content_type)

        if content_type.startswith("image/"):
            prefix = s3_image_prefix
        else:
            prefix = s3_prefix

        user_segment = _sanitize_s3_user_segment(user_id)
        if user_segment:
            s3_key = f"{prefix}/{user_segment}/{file_name}"
            relative_url_path = f"{prefix}/{parse.quote(user_segment)}/{parse.quote(file_name)}"
        else:
            s3_key = f"{prefix}/{file_name}"
            relative_url_path = f"{prefix}/{parse.quote(file_name)}"
        user_meta = {"content_type": content_type}

        put_params = {
            "Bucket": s3_bucket,
            "Key": s3_key,
            "Metadata": user_meta,
            "Body": file_bytes,
        }
        if content_type != "no info":
            put_params["ContentType"] = content_type
        if content_type == "application/pdf":
            put_params["ContentDisposition"] = "inline"

        response = s3_client.put_object(**put_params)
        logger.info("upload response: %s", response)

        url = None
        if sharing_url:
            url = f"{sharing_url.rstrip('/')}/{relative_url_path}"

        return {
            "file_name": file_name,
            "s3_key": s3_key,
            "content_type": content_type,
            "url": url,
        }
    except Exception:
        logger.error("Error uploading to S3: %s", traceback.format_exc())
        return None


ACTIVE_INGESTION_STATUSES = ("STARTING", "IN_PROGRESS")


def _bedrock_agent_client():
    return boto3.client(
        service_name="bedrock-agent",
        region_name=bedrock_region,
    )


def get_active_ingestion_job() -> dict | None:
    """Return an in-flight ingestion job if Knowledge Base sync is already running."""
    if not knowledge_base_id or not data_source_id:
        logger.error("knowledge_base_id or data_source_id is not configured")
        return None

    try:
        bedrock_client = _bedrock_agent_client()
        for status in ACTIVE_INGESTION_STATUSES:
            response = bedrock_client.list_ingestion_jobs(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=data_source_id,
                filters=[
                    {
                        "attribute": "STATUS",
                        "operator": "EQ",
                        "values": [status],
                    }
                ],
                maxResults=1,
                sortBy={
                    "attribute": "STARTED_AT",
                    "order": "DESCENDING",
                },
            )
            summaries = response.get("ingestionJobSummaries") or []
            if not summaries:
                continue
            job = summaries[0]
            logger.info("Active ingestion job found: %s", job)
            return {
                "ingestion_job_id": job.get("ingestionJobId"),
                "status": job.get("status"),
                "started_at": str(job["startedAt"]) if job.get("startedAt") else None,
            }
        return None
    except Exception:
        logger.error("Error listing ingestion jobs: %s", traceback.format_exc())
        raise


def sync_data_source() -> dict | None:
    """Start a Knowledge Base ingestion job for the configured data source."""
    if not knowledge_base_id or not data_source_id:
        logger.error("knowledge_base_id or data_source_id is not configured")
        return None

    try:
        bedrock_client = _bedrock_agent_client()
        response = bedrock_client.start_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
        )
        logger.info("start_ingestion_job response: %s", response)
        job = response.get("ingestionJob", {})
        return {
            "ingestion_job_id": job.get("ingestionJobId"),
            "status": job.get("status"),
        }
    except Exception:
        logger.error("Error syncing data source: %s", traceback.format_exc())
        return None

