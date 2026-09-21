# Release runbook

Host-neutral procedure for cutting a `digital-twins` release. No host paths,
no credentials, and no interpreter-version pin appear here — every command
runs on whatever `python` is on `PATH` and the version resolves through the
package's single-sourced `__version__` (AGENTS.md: version lives only in
`digital_twins/__init__.py`).

Credentialed steps (TestPyPI / PyPI upload, `pip install` round-trip) are
marked **manual, credential-gated** and are run by the releaser, not by the
in-suite check.

---

## 1. Pre-release

Prepare the tree before building anything.

1. **Bump the version.** Edit `digital_twins/__init__.py` and set
   `__version__` to the next release. This is the single source of truth —
   `pyproject.toml` reads it via `[tool.hatch.version]`, and
   `digital-twins --version` / `--version-json` report it. Do not hardcode a
   version anywhere else.
2. **Add a CHANGELOG entry.** Open `CHANGELOG.md`, add a new
   `## [<version>] - <YYYY-MM-DD>` section at the top under the
   `[Keep a Changelog]` format, and list the changes (Added / Changed /
   Fixed / Removed). Follow the existing section style.
3. **Confirm the standing guards are green.** The two permanent guard tests
   must pass before you build:
   - `pytest tests/integration/test_portability.py -v` (host-portability
     guard)
   - `pytest tests/unit/test_knob_docs.py -v` (knob-docs guard)

   Both are required to stay green per AGENTS.md. Fix any failure before
   proceeding.

## 2. Build

Build the sdist + wheel in a **clean venv** so the build does not pick up
host-specific artifacts or a stale environment:

```bash
python -m venv .venv-release
. .venv-release/bin/activate
python -m pip install --upgrade pip build
python -m build
```

`python -m build` produces `dist/digital_twins-<version>-py3-none-any.whl`
and `dist/digital_twins-<version>.tar.gz` through the PEP 517 backend
declared in `pyproject.toml` (hatchling). Confirm both artifacts exist in
`dist/`.

### Building the transition metapackage (0.11.0+)

Since the distribution was renamed to `digital-twins` in 0.11.0, a
**transition metapackage** at `digital-twins-kb/` (top-level dir, its own
`pyproject.toml`, zero-code) is built and uploaded as a *separate* PyPI
dist so the legacy `pip install digital-twins-kb` command keeps resolving
for one release cycle. It is built from its own directory:

```bash
python -m venv .venv-metapkg && . .venv-metapkg/bin/activate
python -m pip install --upgrade pip build
cd digital-twins-kb
python -m build
# produces dist/digital_twins_kb-<version>-py3-none-any.whl
cd ..
```

The metapackage wheel contains only an empty marker module
(`digital_twins_kb/__init__.py`) and carries a single
`Requires-Dist: digital-twins==<version>` line in its METADATA — it
contributes no behavior; pip resolves the real distribution. Both
artifacts (the main `digital-twins` wheel + the `digital-twins-kb`
metapackage wheel) are uploaded to TestPyPI / PyPI in section 4 / 5
above.

## 3. Automated in-suite check

The repository ships an automated, host-neutral build check that runs as part
of the test suite (no venv, no install, no credentials):

```bash
pytest tests/integration/test_pypi_build.py -v
```

It builds the wheel once (PEP 517, via `python -m build --wheel`), then
asserts:

- the wheel exists and is named `digital_twins-<version>-py3-none-any.whl`
  where `<version>` is the single-sourced `digital_twins.__version__`;
- the wheel is a valid ZIP containing the required `.dist-info/METADATA`
  and `.dist-info/WHEEL` members;
- `LICENSE` exists at the repo root and is an MIT license;
- `docs/release-runbook.md` (this file) exists.

This is the gate that must be green before any upload. It deliberately does
**not** `pip install` the wheel or run `pip show` — that round-trip needs a
clean venv and is covered manually below.

## 4. TestPyPI (manual, credential-gated)

Upload to TestPyPI and run the install round-trip. This requires a TestPyPI
account and a trusted host; it is never automated.

```bash
python -m pip install --upgrade twine
# Publish to the TestPyPI index:
python -m twine upload --repository-url https://test.pypi.org/legacy/ dist/*
```

Then verify the round-trip in a fresh venv:

```bash
python -m pip install --index-url https://test.pypi.org/simple/ digital-twins
python -m digital_twins --version
# Confirm the license ships inside the installed distribution:
python -m pip show digital-twins | grep -i license
# Optional: confirm the transition metapackage still resolves (0.11.0+):
#   pip install --index-url https://test.pypi.org/simple/ digital-twins-kb
#   pip show digital-twins-kb   # shows Requires: digital-twins
```
```

Confirm the reported version matches the bumped `__version__` and the
installed dist carries the MIT license. The `pip show` license check is the
manual credential-gated half of the build check; the in-suite test covers the
build + file-existence half.

## 5. PyPI (manual, credential-gated)

Once TestPyPI is verified, publish to the production index. This requires a
PyPI account and an API token — never hardcode the token; export it for the
`twine` command in your environment:

```bash
export TWINE_PASSWORD="<your-pypi-api-token>"
python -m twine upload dist/*
```

`twine` reads the username/token from the standard env (`TWINE_USERNAME`,
`TWINE_PASSWORD`) or `~/.pypirc` — whichever your release environment
provides. Do not commit either.

## 6. Tag

Tag the release commit so the version is discoverable:

```bash
git tag v<version>
git push origin v<version>
```

Use the exact value of the bumped `__version__` after the `v` prefix
(e.g. version `0.5.0` → tag `v0.5.0`).

## 7. Record actual image tags

If the release ships a container image (see the repo's `Dockerfile` /
`docker-compose.yml`), resolve any **placeholder tags** that the build
emitted (e.g. `latest`, an empty `IMAGE_TAG`, or a build-date placeholder)
into the actual pushed image reference.

- Identify the image tag the push actually produced (registry + digest or
  version tag).
- Record it where the release notes / runbook reference the image (replace
  the placeholder with the real tag).
- Confirm the recorded tag resolves on the registry (`docker manifest inspect
  <registry>/<image>:<tag>` or the registry UI) before closing the release.

Do not leave a placeholder tag in a committed artifact; every release record
must name the concrete image tag that was pushed.

---

## Quick checklist

- [ ] `__version__` bumped (single source, `digital_twins/__init__.py`)
- [ ] CHANGELOG entry added
- [ ] `test_portability.py` + `test_knob_docs.py` green
- [ ] `python -m build` in a clean venv → sdist + wheel in `dist/`
- [ ] Transition metapackage built (`cd digital-twins-kb && python -m build`) (0.11.0+)
- [ ] `pytest tests/integration/test_pypi_build.py -v` green (in-suite check)
- [ ] TestPyPI upload + `pip install` round-trip verified (main + metapackage) (manual)
- [ ] PyPI upload with API token (main + metapackage) (manual)
- [ ] `git tag v<version>` pushed
- [ ] Actual image tags recorded (no placeholders left)
