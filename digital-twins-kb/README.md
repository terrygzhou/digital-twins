# digital-twins-kb (transition metapackage)

This is a **thin metapackage** that exists only so that the legacy
`pip install digital-twins-kb` command keeps working for one release
cycle after the PyPI distribution was renamed to `digital-twins` (0.11.0).

It contains **no code** — it merely depends on the real distribution:

```
Requires-Dist: digital-twins==0.11.0
```

Installing `digital-twins-kb` pulls in `digital-twins` and therefore the
`digital-twins` CLI and `digital_twins` import package. The three layers now
share one name:

- PyPI distribution: `digital-twins`
- CLI on PATH: `digital-twins`
- Python import: `digital_twins`

## Migration

If you are still on `digital-twins-kb`, migrate in one step:

```bash
pip install digital-twins
pip uninstall digital-twins-kb
```

This metapackage is **deprecated** and will be retired in a future release.
Its upload procedure is documented in
[`docs/release-runbook.md`](../docs/release-runbook.md).
