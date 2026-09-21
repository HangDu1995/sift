"""Resolve external commands before handing them to subprocess."""

import errno
import os
import shutil


def resolve_executable(name: str) -> str:
    """Return an absolute executable path, preserving missing-command errors."""
    executable = shutil.which(name)
    if executable is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), name)
    return os.path.abspath(executable)
