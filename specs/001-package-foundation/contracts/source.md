# Source Adapter Contract

A source is anything that yields `IngestItem`s. This contract is what makes built-in and user-defined sources behave identically (fail-fast, dedup, audit).

```python
class Capability:            # declared per source (config for custom; code for built-ins)
    runtime: str | None      # agent runtime this source reads (e.g. "hermes", "pi", "dsh"); None = host-neutral
    credential: str | None   # env-var name holding the required secret (None = no credential)
    prefix: str              # stamped onto source_url (e.g. "pi:")

class Source:
    name: str
    capability: Capability

    def prerequisites(self) -> list[str]:
        """Return human-readable list of MISSING prerequisites ([] = ready).
        Checked at enable-time and at run start. Non-empty -> run fails fast (exit 2)."""

    def read(self, since: str | None) -> Iterator[IngestItem]:
        """Yield items newer than `since` (high-water cursor). Must be resumable:
        re-reading after interruption yields the same items with stable keys."""

    def close(self) -> None: ...

def factory(config_entry) -> Source   # the object a config `entrypoint` resolves to
```

## Rules

1. `source_url = capability.prefix + item_key`; `item_key` is stable across runs (it feeds the deterministic point ID).
2. Sources MUST NOT read/write outside their declared inputs; every location comes from config (`extra`).
3. A source with `capability.credential` set MUST check the env var is present in `prerequisites()` and name it when missing.
4. `max_items` / `timeout_s` caps are enforced by the pipeline, not by each source.
5. Custom sources: the config `entrypoint` is imported and called as a factory; import failure = fail-fast with the module path named.

## Built-in registry

`hermes`, `pi`, `dsh`, `paperclip`, `yahoo`, `gmail`, `fs` — registered by name; each declares its capability in code (matching the config surface documented in `config-schema.md`).
