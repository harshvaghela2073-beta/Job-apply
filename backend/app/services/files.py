"""Writing files atomically: a temp file in the same folder, fsync, then ``os.replace``."""

import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

NEW_FILE_MODE = 0o644


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` (creating its folder). Readers see the old or the new file,
    never a partial one. A symlink is written through; an existing file keeps its permissions."""
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        mode = NEW_FILE_MODE
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
