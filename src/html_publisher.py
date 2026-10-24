import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .config import (
    OUT_DIR,
    PUBLIC_HTML_DIR,
    PUBLIC_BASE_URL,
    PUBLIC_HTML_URL_PREFIX,
)

LOCAL_HTML_PREFIX = "local:"
_HTML_FILENAME_RE = re.compile(r"chat_[0-9a-f]{32}\.html")
_TEMP_HTML_FILENAME_RE = re.compile(
    r"\.chat_[0-9a-f]{32}\.html\.[0-9a-f]{32}\.tmp"
)
_STALE_TEMP_HTML_AGE_SECONDS = 24 * 60 * 60


def _write_html_atomically(html_text: str, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)

    filename = f"chat_{uuid.uuid4().hex}.html"
    filepath = os.path.join(output_dir, filename)
    temp_path = os.path.join(output_dir, f".{filename}.{uuid.uuid4().hex}.tmp")
    temp_created = False

    try:
        with open(temp_path, "x", encoding="utf-8") as temp_file:
            temp_created = True
            written = temp_file.write(html_text)
            if written != len(html_text):
                raise OSError("Incomplete HTML write")
            temp_file.flush()

        os.replace(temp_path, filepath)
        temp_path = None
    finally:
        if temp_path is not None and temp_created:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass

    return filename


def publish_html(html_text: str) -> str:
    if not PUBLIC_HTML_DIR or not PUBLIC_BASE_URL:
        raise RuntimeError(
            "Public HTML mode requires TCD_PUBLIC_HTML_DIR and TCD_PUBLIC_BASE_URL"
        )

    filename = _write_html_atomically(html_text, PUBLIC_HTML_DIR)

    return f"{PUBLIC_BASE_URL}{PUBLIC_HTML_URL_PREFIX}/{filename}"


def publish_html_local(html_text: str) -> str:
    """Write HTML into the project output directory and return a local cache reference."""
    filename = _write_html_atomically(html_text, OUT_DIR)
    return f"{LOCAL_HTML_PREFIX}{filename}"


def cleanup_stale_html_temp_files() -> int:
    """Remove only publisher temp files older than a day from configured output dirs."""
    roots = []
    for configured_dir in (OUT_DIR, PUBLIC_HTML_DIR):
        if not configured_dir:
            continue
        try:
            root = Path(configured_dir).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if root not in roots:
            roots.append(root)

    cutoff = time.time() - _STALE_TEMP_HTML_AGE_SECONDS
    removed = 0
    for root in roots:
        try:
            for path in root.iterdir():
                if not _TEMP_HTML_FILENAME_RE.fullmatch(path.name):
                    continue
                try:
                    if path.is_symlink() or not path.is_file() or path.stat().st_mtime >= cutoff:
                        continue
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        except OSError:
            continue
    return removed


def is_local_html_reference(html_ref: str | None) -> bool:
    if not isinstance(html_ref, str) or not html_ref.startswith(LOCAL_HTML_PREFIX):
        return False
    filename = html_ref[len(LOCAL_HTML_PREFIX):]
    return bool(_HTML_FILENAME_RE.fullmatch(filename))


def local_html_relative_path(html_ref: str | None) -> str | None:
    if not is_local_html_reference(html_ref):
        return None
    filename = html_ref[len(LOCAL_HTML_PREFIX):]
    return f"_out/{filename}"


def local_published_html_status(html_url: str | None) -> bool | None:
    """Return local availability, or None when this publisher cannot verify the URL."""
    if not html_url:
        return None

    if isinstance(html_url, str) and html_url.startswith(LOCAL_HTML_PREFIX):
        if not is_local_html_reference(html_url):
            return False
        root = Path(OUT_DIR).resolve()
        html_path = (root / html_url[len(LOCAL_HTML_PREFIX):]).resolve()
        try:
            return html_path.parent == root and html_path.is_file() and html_path.stat().st_size > 0
        except OSError:
            return False

    if not PUBLIC_BASE_URL or not PUBLIC_HTML_DIR:
        return None

    try:
        base = urlsplit(PUBLIC_BASE_URL)
        target = urlsplit(html_url)
    except ValueError:
        return None

    if (target.scheme, target.netloc) != (base.scheme, base.netloc):
        return None

    expected_path_prefix = f"{base.path.rstrip('/')}{PUBLIC_HTML_URL_PREFIX.rstrip('/')}/"
    if not target.path.startswith(expected_path_prefix) or target.query or target.fragment:
        return None

    filename = target.path[len(expected_path_prefix):]
    if not re.fullmatch(r"chat_[0-9a-f]{32}\.html", filename):
        return None

    try:
        root = Path(PUBLIC_HTML_DIR).resolve()
        html_path = (root / filename).resolve()
        if html_path.parent != root or not html_path.is_file():
            return False
        with html_path.open("rb") as html_file:
            return bool(html_file.read(1))
    except (OSError, RuntimeError):
        return False
