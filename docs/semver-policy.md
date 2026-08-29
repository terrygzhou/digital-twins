# Versioning Policy

`major.minor.patch`, single-sourced in `digital_twins/__init__.py`
(`__version__`). `digital-twins --version` and `--version-json` read it.

## What each bump means

| Change | Bump | Example |
|---|---|---|
| New knob or new feature (additive, config backward-compatible) | **minor** | 0.4.0 → 0.5.0 |
| Bug fix, doc fix, dependency patch | **patch** | 0.5.0 → 0.5.1 |
| **Config-breaking** change | **major** + migration note | 0.5.0 → 1.0.0 |
| Knob deprecation (one-run warning, replacement named) | **minor** | 0.5.0 → 0.6.0 |

## Config-breaking — the precise trigger set

Any of the following is `major`, and the release notes **must** carry a
migration note:

1. Removing a knob from the `KNOBS` registry.
2. Renaming a knob (old key no longer resolves).
3. Changing a knob's declared type.
4. Changing a knob's default semantics.
5. Changing the env-var mapping (`KB_` prefix / `__` nesting rule).

## The deprecation mechanism

A deprecated knob emits a **one-run warning** naming the replacement (the next
read logs the warning once; it does not repeat every run). 005 documents the
mechanism but deprecates no real knob; the behavior is demonstrated by a test
fixture, not by a shipped knob.

## Mapping to the tracker

A config-breaking change is proposed via
`.github/ISSUE_TEMPLATE/config-breaking-change.yml`, which carries a **required**
`version_impact` field (vocabulary `major`|`minor`|`patch`) and a **required**
`migration_note` field. At release, the runbook cross-checks that the
changelog's breaking entry forces the major bump
(`docs/release-runbook.md`).

## Changelog linkage

Every release since 0.1.0 has a `CHANGELOG.md` entry (Keep a Changelog
sections). A breaking release's notes carry the migration note. This policy is
the machine- and human-readable source for that rule.
