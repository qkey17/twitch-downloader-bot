import os
import re
import time
import uuid
import hashlib
import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from .file_lock import try_lock_file
from .resource_usage import log_resources

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


def _public_path(filename=""):
    base_path = urlsplit(PUBLIC_BASE_URL).path
    return '/' + '/'.join(part for part in f'{base_path}/{PUBLIC_HTML_URL_PREFIX}/{filename}'.split('/') if part)


def public_html_url(filename: str) -> str:
    """Canonical URL for new output; stored legacy references stay unchanged."""
    if not _HTML_FILENAME_RE.fullmatch(filename):
        raise ValueError("Invalid HTML filename")
    base = urlsplit(PUBLIC_BASE_URL)
    return urlunsplit((base.scheme, base.netloc, _public_path(filename), '', ''))


def public_html_filename(html_url: str | None) -> str | None:
    """Map this publisher's URLs, including pre-HTTPS and duplicate-slash URLs."""
    if not isinstance(html_url, str) or not PUBLIC_BASE_URL or not PUBLIC_HTML_DIR:
        return None
    try:
        base, target = urlsplit(PUBLIC_BASE_URL), urlsplit(html_url)
        # An HTTPS migration keeps the exact authority (including any explicit
        # port). Only old HTTP references are compatible; do not broaden hosts,
        # infer ports, or accept an HTTPS reference under an HTTP configuration.
        scheme_matches = (target.scheme == base.scheme
                          or (base.scheme == 'https' and target.scheme == 'http'))
        if (target.scheme not in ('http', 'https')
                or not scheme_matches or target.netloc != base.netloc
                or '?' in html_url or '#' in html_url
                or re.search(r'[\x00-\x20\x7f]', html_url)):
            return None
        path = re.sub(r'/+', '/', target.path)
        prefix = _public_path().rstrip('/') + '/'
        filename = path[len(prefix):]
        if path.startswith(prefix) and _HTML_FILENAME_RE.fullmatch(filename):
            return filename
    except (TypeError, ValueError):
        pass
    return None


def _write_html_atomically(html_text, output_dir: str, cancel_check=None) -> str:
    os.makedirs(output_dir, exist_ok=True)

    filename = f"chat_{uuid.uuid4().hex}.html"
    filepath = os.path.join(output_dir, filename)
    temp_path = os.path.join(output_dir, f".{filename}.{uuid.uuid4().hex}.tmp")
    temp_created = False

    try:
        with open(temp_path, "x", encoding="utf-8") as temp_file:
            temp_created = True
            if not try_lock_file(temp_file):
                raise OSError("Could not reserve HTML publication temp file")
            if callable(html_text):
                html_text(temp_file)
            else:
                written = temp_file.write(html_text)
                if written != len(html_text):
                    raise OSError("Incomplete HTML write")
            temp_file.flush()
            html_bytes = os.fstat(temp_file.fileno()).st_size

        if cancel_check is not None and cancel_check():
            raise RuntimeError("Загрузка была отменена.")
        os.replace(temp_path, filepath)
        temp_path = None
        log_resources("html_published", html_bytes=html_bytes)
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

    return public_html_url(filename)


def publish_html_local(html_text: str) -> str:
    """Write HTML into the project output directory and return a local cache reference."""
    filename = _write_html_atomically(html_text, OUT_DIR)
    return f"{LOCAL_HTML_PREFIX}{filename}"


def publish_html_stream(write_content, *, local=False, cancel_check=None, publication=None) -> str:
    """Atomically publish a callback-written HTML without materializing it."""
    if publication is not None:
        return _publish_registered(write_content,publication,cancel_check)
    if not local and (not PUBLIC_HTML_DIR or not PUBLIC_BASE_URL):
        raise RuntimeError("Public HTML mode requires TCD_PUBLIC_HTML_DIR and TCD_PUBLIC_BASE_URL")
    directory = OUT_DIR if local else PUBLIC_HTML_DIR
    filename = _write_html_atomically(write_content, directory, cancel_check=cancel_check)
    return f"{LOCAL_HTML_PREFIX}{filename}" if local else public_html_url(filename)


def _publish_registered(write_content, publication, cancel_check):
    if publication._lease is None or not publication._lease_created:
        raise ValueError("HTML generation has not been reserved")
    final = publication.path()
    if final.exists():
        raise FileExistsError("Generation filename already exists")
    if not re.fullmatch(rf"\.{re.escape(publication.filename)}\.[0-9a-f]{{32}}\.tmp",publication.temp_name):
        raise ValueError("Unsafe HTML temporary filename")
    temp = final.parent / publication.temp_name
    created = False
    identity = None
    digest = hashlib.sha256()
    size = 0

    def check_cancelled():
        if cancel_check is not None and cancel_check():
            raise RuntimeError("Загрузка была отменена.")

    def still_owned():
        try:
            current = temp.stat(follow_symlinks=False)
            return identity == (current.st_dev, current.st_ino)
        except FileNotFoundError:
            return False

    try:
        with temp.open('xb') as raw:
            created = True
            owned = os.fstat(raw.fileno())
            identity = (owned.st_dev, owned.st_ino)
            if not try_lock_file(raw):
                raise OSError("Could not reserve HTML publication temp file")

            class Output:
                def write(self, text):
                    nonlocal size
                    check_cancelled()
                    data = text.replace('\n',os.linesep).encode('utf-8')
                    if raw.write(data) != len(data):
                        raise OSError("Incomplete HTML write")
                    digest.update(data)
                    size += len(data)
                    return len(text)

            write_content(Output())
            raw.flush()
            os.fsync(raw.fileno())
        check_cancelled()
        if not size:
            raise ValueError("Empty HTML output")
        publication.stage(size,digest.hexdigest())  # Durable proof precedes final-file visibility.
        check_cancelled()
        publication.path()  # Revalidate configured root after any blocking DB wait.
        if not still_owned():
            raise ValueError("HTML temporary file was replaced")
        os.link(temp,final)  # Atomic visibility, no overwrite even on a filename collision.
        if os.name != 'nt':
            directory_fd = os.open(final.parent,os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        publication._published = True
        log_resources('html_published',html_bytes=size)
        return publication.ref
    finally:
        if created:
            if still_owned():
                temp.unlink(missing_ok=True)
            elif temp.exists() or temp.is_symlink():
                logging.getLogger(__name__).warning("Replaced HTML temporary file retained")


def cleanup_stale_html_temp_files(protected=()) -> int:
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
                if path in protected:
                    continue
                try:
                    if path.is_symlink() or not path.is_file() or path.stat().st_mtime >= cutoff:
                        continue
                    with path.open("r+b") as handle:
                        if not try_lock_file(handle):
                            continue
                        if os.fstat(handle.fileno()).st_mtime >= cutoff:
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

    filename = public_html_filename(html_url)
    if filename is None:
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
