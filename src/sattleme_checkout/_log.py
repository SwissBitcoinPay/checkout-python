"""Where the package's warnings go: a `logging.Logger` (or anything with its `warning`)."""

from __future__ import annotations

from typing import Protocol


class Logger(Protocol):
    """Warnings only, never given a secret: `logging.getLogger(...)` or a `LoggerAdapter`."""

    def warning(self, msg: str, /, *args: object) -> object: ...
