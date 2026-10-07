# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
utils/file_ops.py — Phidipus v1.0.2
Safe filesystem helpers with path-traversal protection.

Security design:
  - safe_join() resolves symlinks and verifies the resulting absolute path
    is a strict descendant of the declared root, blocking ``../`` traversal,
    absolute injections, and symlink escapes.
  - All write helpers route through atomic_file.atomic_write_* so partial
    writes are never visible.
  - No shell expansion or subprocess calls anywhere in this module.

Used by:
  patcher/runtime_patcher.py  — patched file read/write (R-18 path guards)
  skills/skill_loader.py      — generated skill file access
  skill_validator/skill_signer.py — .sig file placement
  memory/episodic_memory.py   — episode file access

Patch v9.11.2:
  TASK-5A  Hoisted ``import re`` to module level (was inside is_safe_filename).
  TASK-5B  PROHIBITED_WRITE_PREFIXES normalised to POSIX-only strings.
           check_write_allowed() already uses Path.as_posix() for comparison,
           so os.sep variants were redundant and have been removed.
           All existing defense-in-depth checks are preserved.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Union

from utils.atomic_file import atomic_write_bytes, atomic_write_text

# ---------------------------------------------------------------------------
# Path traversal protection
# ---------------------------------------------------------------------------

class PathTraversalError(ValueError):
    """Raised when a resolved path escapes its declared root."""


def safe_join(root: Union[str, Path], *parts: str) -> Path:
    """
    Join *parts* onto *root* and verify the result stays inside *root*.

    All components are joined with Path(), then resolved to an absolute path
    (following symlinks).  If the resolved path is not a descendant of the
    resolved *root*, :exc:`PathTraversalError` is raised.

    Args:
        root:   The trusted base directory.  Must exist and be a directory.
        *parts: Path fragments to join.  May come from untrusted sources.

    Returns:
        Resolved absolute Path guaranteed to be inside *root*.

    Raises:
        PathTraversalError: if the joined path escapes *root*.
        NotADirectoryError: if *root* is not a directory.

    Example::

        base = Path("/data/skills/generated")
        path = safe_join(base, user_supplied_name + ".py")
        # Raises if user_supplied_name == "../../etc/passwd"
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Root is not a directory: {root}")

    # FIX B-09: Path() raises ValueError for null bytes and other invalid
    # chars. Wrap as PathTraversalError for consistent error handling.
    try:
        candidate = Path(root, *parts)
    except ValueError as exc:
        raise PathTraversalError(
            f"Path component is invalid: {exc}"
        ) from exc
    # resolve() follows all symlinks so a symlink pointing outside root is caught.
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError) as exc:  # FIX B-09: also catch ValueError
        raise PathTraversalError(
            f"Could not resolve path {candidate!r}: {exc}"
        ) from exc

    # Check strict descent: resolved must start with root + separator.
    # Using str comparison after resolve() is safe because both are absolute.
    root_str = str(root)
    resolved_str = str(resolved)
    if not (resolved_str == root_str or resolved_str.startswith(root_str + os.sep)):
        raise PathTraversalError(
            f"Path {resolved_str!r} escapes root {root_str!r}"
        )

    return resolved


def is_safe_filename(name: str) -> bool:
    """
    Return True iff *name* is a plain filename component with no path separators,
    no null bytes, no leading dots (hidden files), and only alphanumerics,
    underscores, hyphens, and dots.

    Does NOT check whether the file exists.
    """
    if not name:
        return False
    if "\x00" in name:
        return False
    if os.sep in name or (os.altsep and os.altsep in name):
        return False
    if name.startswith("."):
        return False
    # Allow: a-z A-Z 0-9 _ - .
    # re is imported at module level (v9.11.2 TASK-5A).
    return bool(re.fullmatch(r"[A-Za-z0-9_\-.]+", name))


# ---------------------------------------------------------------------------
# Prohibited path prefixes (R-18 — patcher must not write to these)
# ---------------------------------------------------------------------------
# v9.11.2 TASK-5B: POSIX-only strings.  check_write_allowed() converts the
# resolved relative path via Path.as_posix() before comparison, so os.sep
# variants are redundant.  The three POSIX prefixes below cover all platforms.
# Defense-in-depth: the traversal check in check_write_allowed() runs first
# and catches all escape attempts regardless of separator convention.

PROHIBITED_WRITE_PREFIXES: tuple[str, ...] = (
    "sandbox/",
    "security/",
    "patcher/",
)


def check_write_allowed(path: Union[str, Path], root: Union[str, Path]) -> None:
    """
    Raise :exc:`PermissionError` if *path* (relative to *root*) falls under a
    prohibited write prefix as defined in PROHIBITED_WRITE_PREFIXES.

    Used by :mod:`patcher.runtime_patcher` to enforce R-18.

    Args:
        path: The candidate target path (absolute or relative to *root*).
        root: Project root.

    Raises:
        PermissionError: if the write is prohibited.
        PathTraversalError: if path escapes root.
    """
    root = Path(root).resolve()
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    # FIX B-10: wrap ValueError from null bytes
    try:
        resolved = path.resolve()
    except ValueError as exc:
        raise PermissionError(f"Invalid path: {exc}") from exc

    # Ensure within root first
    root_str = str(root)
    resolved_str = str(resolved)
    if not (resolved_str == root_str or resolved_str.startswith(root_str + os.sep)):
        # FIX B-10: raise PermissionError (not PathTraversalError) for
        # consistent interface — docstring documents PermissionError.
        raise PermissionError(
            f"Write target {resolved_str!r} escapes project root {root_str!r}"
        )

    # Compute relative path for prefix check
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        # FIX B-10: PermissionError, not PathTraversalError
        raise PermissionError(
            f"Cannot compute relative path for {resolved_str!r} under {root_str!r}"
        )

    # as_posix() normalises platform separators to "/" so POSIX prefixes
    # match correctly on all platforms (Linux, macOS, Windows).
    rel_posix = rel.as_posix() + "/"
    for prefix in PROHIBITED_WRITE_PREFIXES:
        if rel_posix.startswith(prefix):
            raise PermissionError(
                f"R-18: Writes to '{prefix}' are prohibited. "
                f"Attempted target: {rel_posix!r}"
            )


# ---------------------------------------------------------------------------
# Safe read helpers
# ---------------------------------------------------------------------------

def read_bytes(path: Union[str, Path]) -> bytes:
    """
    Read and return the raw bytes of *path*.

    Raises:
        FileNotFoundError: if *path* does not exist.
        OSError:           on read failure.
    """
    return Path(path).read_bytes()


def read_text(path: Union[str, Path], *, encoding: str = "utf-8") -> str:
    """
    Read and return the text content of *path*.

    Raises:
        FileNotFoundError: if *path* does not exist.
        OSError:           on read failure.
        UnicodeDecodeError: if the file is not valid *encoding*.
    """
    return Path(path).read_text(encoding=encoding)


def read_lines(
    path: Union[str, Path],
    *,
    encoding: str = "utf-8",
    strip: bool = True,
    skip_empty: bool = True,
) -> list[str]:
    """
    Read *path* and return its lines as a list.

    Args:
        path:       File to read.
        encoding:   Text encoding.
        strip:      Strip leading/trailing whitespace from each line.
        skip_empty: Skip blank lines after stripping.
    """
    raw = read_text(path, encoding=encoding)
    lines = raw.splitlines()
    if strip:
        lines = [ln.strip() for ln in lines]
    if skip_empty:
        lines = [ln for ln in lines if ln]
    return lines


# ---------------------------------------------------------------------------
# Safe write helpers (delegate to atomic_file for crash safety)
# ---------------------------------------------------------------------------

def write_bytes(
    path: Union[str, Path],
    data: bytes,
    *,
    mode: int = 0o644,
) -> None:
    """Atomically write *data* to *path*."""
    atomic_write_bytes(path, data, mode=mode)


def write_text(
    path: Union[str, Path],
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int = 0o644,
) -> None:
    """Atomically write *text* to *path*."""
    atomic_write_text(path, text, encoding=encoding, mode=mode)


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

def ensure_dir(path: Union[str, Path], *, mode: int = 0o755) -> Path:
    """
    Create *path* and all intermediate directories if they do not exist.

    Equivalent to ``mkdir -p`` but returns the resolved Path.

    Args:
        path: Directory to create.
        mode: Permission bits for newly created directories.

    Returns:
        Resolved absolute path to the directory.
    """
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True, mode=mode)
    return p.resolve()


def safe_delete(path: Union[str, Path]) -> bool:
    """
    Delete *path* if it exists.  Does not raise if already absent.

    Returns:
        True if the file was deleted, False if it was not found.

    Raises:
        OSError: on unexpected deletion failures (e.g. permission denied).
    """
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return False


def list_files(
    directory: Union[str, Path],
    *,
    pattern: str = "*",
    recursive: bool = False,
) -> list[Path]:
    """
    Return a sorted list of files in *directory* matching *pattern*.

    Args:
        directory: Directory to search.
        pattern:   Glob pattern (e.g. ``"*.py"``).
        recursive: If True, search recursively (``rglob``).

    Returns:
        Sorted list of absolute Path objects for matching files.
    """
    d = Path(directory)
    if not d.is_dir():
        return []
    fn = d.rglob if recursive else d.glob
    return sorted(p for p in fn(pattern) if p.is_file())


def copy_file(
    src: Union[str, Path],
    dst: Union[str, Path],
    *,
    mode: int = 0o644,
) -> None:
    """
    Copy *src* to *dst* atomically (read src → write bytes to dst).

    Raises:
        FileNotFoundError: if *src* does not exist.
        OSError:           on read/write failure.
    """
    data = read_bytes(src)
    atomic_write_bytes(dst, data, mode=mode)
