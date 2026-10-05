"""Descriptor-anchored cleanup for private local test directories (Python 3.9+)."""

import os
from pathlib import Path
import stat


def open_directory_chain(path):
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in Path(os.path.abspath(path)).parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=current)
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _identity(entry):
    return entry.st_dev, entry.st_ino, stat.S_IFMT(entry.st_mode)


def _require_entry(parent, name, original):
    if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(original):
        raise RuntimeError(f"Fixture cleanup refused changed entry: {name}")


def _empty_directory(handle):
    for name in os.listdir(handle):
        original = os.stat(name, dir_fd=handle, follow_symlinks=False)
        if stat.S_ISDIR(original.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handle)
            try:
                if _identity(os.fstat(child)) != _identity(original):
                    raise RuntimeError(f"Fixture cleanup refused changed directory: {name}")
                _empty_directory(child)
                _require_entry(handle, name, os.fstat(child))
                os.rmdir(name, dir_fd=handle)
            finally:
                os.close(child)
        else:
            _require_entry(handle, name, original)
            os.unlink(name, dir_fd=handle)


def remove_tree(parent, name, root, *, missing_ok=False):
    """Keep caller-owned FDs open; refuse replacement or rename of the root."""
    original = os.fstat(root)
    try:
        _require_entry(parent, name, original)
    except FileNotFoundError:
        # A topology child may already have removed this exact held directory.
        # APFS keeps a removed directory's fstat link count, so require only
        # absence and an empty held directory. No deletion occurs in this case.
        if missing_ok and not os.listdir(root):
            return
        raise
    _empty_directory(root)
    _require_entry(parent, name, original)
    os.rmdir(name, dir_fd=parent)


class OwnedDirectory:
    """Capture a run immediately after creation and release handles in finally."""

    def __init__(self, path):
        self.name = Path(path).name
        self.parent = open_directory_chain(Path(path).parent)
        try:
            self.root = os.open(self.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=self.parent)
        except BaseException:
            os.close(self.parent)
            raise

    def remove(self, *, missing_ok=False):
        remove_tree(self.parent, self.name, self.root, missing_ok=missing_ok)

    def close(self):
        if self.root is not None:
            os.close(self.root)
            os.close(self.parent)
            self.root = None
