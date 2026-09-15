"""Explicit, opt-in, exact-session Markdown archives with no discovery scan."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from suiteharness.sessions.models import SessionIdentity, canonical_json


class MarkdownArchive:
    """Store one portable UTF-8 Markdown document for the full supplied identity.

    Authorization belongs to the host. Use a host-owned root that untrusted
    local users cannot rename or mutate concurrently. Existing symbolic links,
    junctions, reparse points and non-regular/hard-linked files are rejected.
    Disabled store/retrieve/delete perform no filesystem operations; delete
    returns False and disabling never removes previously stored archives.
    Retrieval does literal, case-insensitive paragraph filtering within exactly
    one document; an empty query returns its bounded prefix.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        long_term_enabled: bool = False,
        max_file_bytes: int = 128 * 1024,
    ) -> None:
        if type(max_file_bytes) is not int or not 1 <= max_file_bytes <= 4 * 1024 * 1024:
            raise ValueError("max_file_bytes must be between 1 and 4194304")
        if type(long_term_enabled) is not bool:
            raise ValueError("long_term_enabled must be a boolean")
        # Do not resolve/stat the root here: disabled archives must do no I/O.
        self.root = Path(os.path.abspath(root))
        self.long_term_enabled = long_term_enabled
        self.max_file_bytes = max_file_bytes

    @staticmethod
    def _check_component(path: Path, *, directory: bool) -> os.stat_result | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("archive root/path must not contain links or reparse points")
        if directory:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("archive root must contain only directories")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("archive must be a regular file without hard links")
        return info

    def _path(self, identity: SessionIdentity, *, create: bool = False) -> Path:
        # Revalidate even a model_copy/model_construct identity before deriving
        # the filename. JSON framing prevents delimiter/sanitization collisions.
        checked = SessionIdentity.model_validate(identity.model_dump())
        digest = hashlib.sha256(canonical_json(checked.model_dump()).encode("utf-8")).hexdigest()
        for directory in (*reversed(self.root.parents), self.root):
            self._check_component(directory, directory=True)
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            for directory in (*reversed(self.root.parents), self.root):
                self._check_component(directory, directory=True)
        path = self.root / f"{digest}.md"
        if path.parent != self.root:
            raise ValueError("archive path escaped configured root")
        self._check_component(path, directory=False)
        return path

    def store(self, identity: SessionIdentity, text: str) -> None:
        if not self.long_term_enabled:
            return
        if not isinstance(text, str):
            raise ValueError("archive text must be a string")
        encoded = text.encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            raise ValueError("archive text exceeds configured byte size limit")
        destination = self._path(identity, create=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".archive-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            self._path(identity)
            os.replace(temporary, destination)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def retrieve(self, identity: SessionIdentity, query: str = "", max_chars: int = 4000) -> str:
        if not self.long_term_enabled:
            return ""
        if type(max_chars) is not int or max_chars < 0:
            raise ValueError("max_chars must be a nonnegative integer")
        if not isinstance(query, str) or len(query) > 4000:
            raise ValueError("query must be a string of at most 4000 characters")
        path = self._path(identity)
        before = self._check_component(path, directory=False)
        if before is None or max_chars == 0:
            return ""
        if before.st_size > self.max_file_bytes:
            raise ValueError("archive exceeds configured byte size limit")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            actual = os.fstat(source.fileno())
            if (not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1
                or (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino)):
                raise ValueError("archive changed or is not a regular file")
            raw = source.read(self.max_file_bytes + 1)
        if len(raw) > self.max_file_bytes:
            raise ValueError("archive exceeds configured byte size limit")
        document = raw.decode("utf-8")
        if query.strip():
            needle = query.strip().casefold()
            document = "\n\n".join(part for part in document.split("\n\n") if needle in part.casefold())
        return document[:min(max_chars, self.max_file_bytes)]

    def delete(self, identity: SessionIdentity) -> bool:
        if not self.long_term_enabled:
            return False
        path = self._path(identity)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True
