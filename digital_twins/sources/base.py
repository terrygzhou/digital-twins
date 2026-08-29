"""Source adapter contract (contracts/source.md)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class Capability:
    """Declared per source: config for custom sources, code for built-ins."""

    runtime: str | None    # agent runtime this source reads (None = host-neutral)
    credential: str | None  # env-var name holding the required secret
    prefix: str            # stamped onto source_url (e.g. "pi:")


@dataclass
class IngestItem:
    """One unit a source yields. `key` is stable across runs and feeds the
    deterministic point ID; `ts` (ISO-8601 UTC) drives the high-water cursor."""

    key: str
    content: str
    ts: str
    metadata: dict = field(default_factory=dict)


class Source:
    """Contract every built-in and user-defined source fulfils."""

    name: str
    capability: Capability

    def prerequisites(self) -> list:
        """Human-readable list of MISSING prerequisites ([] = ready)."""
        raise NotImplementedError

    def read(self, since: str | None) -> Iterator[IngestItem]:
        """Yield items newer than `since`; resumable, stable keys."""
        raise NotImplementedError

    def close(self) -> None:
        pass
