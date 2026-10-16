"""Windows file-lock detection for the unattended sync.

Why this exists: Vaish reviews tailored resumes by opening the PDF in Acrobat,
which holds the file with a share-mode denial. git cannot then replace that file
-- `error: unable to unlink old '...Resume.pdf': Invalid argument` -- and a
`git pull --rebase --autostash` that dies mid-checkout leaves the tree
half-applied with the autostash unrecoverable. Reviewing resumes in a viewer IS
the intended workflow, so this is a recurring collision, not a freak event
(measured 2026-08-19: 6 scheduled runs, 5 ok, 1 lost to exactly this).

The probe asks for the access git needs -- including DELETE, because the failure
is an unlink, and a viewer's share mode typically permits reading while denying
delete. Probing with GENERIC_READ alone would succeed and miss the lock.

Nothing else in this repo handles file locks; SQLite's busy_timeout is a
different mechanism for a different resource.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# The two "someone else is holding it" codes. Every OTHER error (not found,
# access denied by ACL, path too long) means something we should not translate
# into "locked" -- a gate that fires on the wrong condition is worse than none.
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE = ctypes.c_void_p(-1).value

TOAST_PS1 = Path(__file__).resolve().parent / "toast.ps1"

if IS_WINDOWS:
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


def _open(path: Path, access: int, share: int) -> int:
    return _k32.CreateFileW(str(path), access, share, None, _OPEN_EXISTING,
                            _FILE_ATTRIBUTE_NORMAL, None)


def is_locked(path: str | Path) -> bool:
    """Can we NOT get the exclusive access git needs to replace this file?

    True only on a genuine sharing/lock violation. Never raises: a probe that
    can throw would turn the safety gate into a new failure mode.
    """
    if not IS_WINDOWS:
        return False  # no mandatory locking; git replaces open files happily
    try:
        p = Path(path)
        if not p.is_file():
            return False
        h = _open(p, _GENERIC_READ | _GENERIC_WRITE | _DELETE, 0)
        if h == _INVALID_HANDLE:
            return ctypes.get_last_error() in (ERROR_SHARING_VIOLATION,
                                               ERROR_LOCK_VIOLATION)
        _k32.CloseHandle(h)
        return False
    except Exception:
        return False


def locked_among(paths, root: str | Path = ".") -> list[str]:
    """Which of `paths` (repo-relative) are locked. Sorted, deduped."""
    root = Path(root)
    hits = set()
    for rel in set(paths):
        if is_locked(root / rel):
            hits.add(str(rel).replace("\\", "/"))
    return sorted(hits)


def wait_for_unlock(paths, root: str | Path = ".", attempts: int = 5,
                    delay: float = 20.0) -> list[str]:
    """Poll until nothing is locked; return whatever is STILL locked.

    Bounded on purpose. A viewer lock lasts as long as Vaish is reading, so this
    loop is not the thing that outlasts it -- the scheduler's restart ladder is.
    This only absorbs a transient toucher (an indexer, an antivirus scan).
    """
    still = locked_among(paths, root)
    for _ in range(max(0, attempts - 1)):
        if not still:
            return []
        time.sleep(delay)
        still = locked_among(paths, root)
    return still


def notify(title: str, body: str, timeout_s: float = 25) -> bool:
    """Best-effort desktop toast. Returns whether it went out.

    Swallows everything. This is the alert channel; an alert channel that can
    crash the run it is reporting on is worse than no alert.
    """
    if not IS_WINDOWS or not TOAST_PS1.is_file():
        return False
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(TOAST_PS1), "-Title", title, "-Body", body],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_s)
        return proc.returncode == 0
    except Exception as exc:
        print(f"    (toast unavailable: {str(exc)[:80]})")
        return False


@contextmanager
def hold(path: str | Path):
    """TEST ONLY: hold `path` the way a PDF viewer does (deny write/delete).

    Raises if the handle cannot be taken. A test whose lock silently failed to
    apply would pass vacuously, asserting nothing -- the exact shape of a
    defence you have only ever seen pass.
    """
    if not IS_WINDOWS:
        raise RuntimeError("winlock.hold is Windows-only")
    h = _open(Path(path), _GENERIC_READ, _FILE_SHARE_READ)
    if h == _INVALID_HANDLE:
        raise RuntimeError(f"could not hold {path} (err={ctypes.get_last_error()})")
    try:
        yield
    finally:
        _k32.CloseHandle(h)
