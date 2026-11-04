"""`fs` source: scope filter — supported types OR files below the size cap.

The filter keeps document/text types at any size and any *small* file, while
dropping only files that are both an unsupported type AND at/above the cap
(large binaries: videos, archives, executables, CAD).
"""

from digital_twins.sources import build
from digital_twins.sources.fs import FsSource, _DEFAULT_SUPPORTED_EXTS


def _mk(files: dict[str, int], tmp_path, **extra):
    """Create `files` ({relpath: size_bytes}) under tmp_path and return a source."""
    for rel, size in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x00" * size)
    src = build("fs", {"extra": {"dir": str(tmp_path), **extra}})
    return src


def test_capability_prefix():
    src = build("fs", {"extra": {"dir": "/nonexistent"}})
    assert src.capability.prefix == "fs:"
    assert src.name == "fs"


def test_missing_dir_is_prerequisite(tmp_path):
    src = build("fs", {"extra": {"dir": str(tmp_path / "nope")}})
    missing = src.prerequisites()
    assert len(missing) == 1
    assert "sources.fs.extra.dir" in missing[0]


def test_supported_ext_ingested_even_when_large(tmp_path):
    # 50 MB .pdf is supported -> kept
    src = _mk({"big.pdf": 50 * 1000 * 1000, "small.bin": 100}, tmp_path)
    keys = [i.key for i in src.read(None)]
    assert "big.pdf" in keys
    assert "small.bin" in keys  # < 10MB -> kept too


def test_unsupported_large_dropped(tmp_path):
    # 300 MB .mov is unsupported AND >= cap -> dropped
    src = _mk({"huge.mov": 300 * 1000 * 1000, "keep.txt": 100}, tmp_path)
    keys = [i.key for i in src.read(None)]
    assert "huge.mov" not in keys
    assert "keep.txt" in keys


def test_unsupported_small_kept(tmp_path):
    # unsupported but below the cap -> kept
    src = _mk({"notes.dat": 500}, tmp_path)
    assert [i.key for i in src.read(None)] == ["notes.dat"]


def test_boundary_is_strictly_less_than(tmp_path):
    cap = 1000
    src = _mk({"eq.bin": cap, "below.bin": cap - 1}, tmp_path, max_bytes=cap)
    keys = set(i.key for i in src.read(None))
    assert "below.bin" in keys
    assert "eq.bin" not in keys  # equal to cap is NOT below it


def test_supported_exts_overridable(tmp_path):
    # restrict the supported set to just .txt -> a 20 MB .mov is dropped
    src = _mk({"movie.mov": 20 * 1000 * 1000}, tmp_path,
              supported_exts=[".txt"])
    keys = [i.key for i in src.read(None)]
    assert "movie.mov" not in keys


def test_filter_can_be_disabled(tmp_path):
    # filter_enabled False -> everything is read regardless of type/size
    src = _mk({"huge.mov": 300 * 1000 * 1000}, tmp_path, filter_enabled=False)
    assert [i.key for i in src.read(None)] == ["huge.mov"]


def test_env_style_string_coercion(tmp_path):
    # values arriving as strings (from env) coerce correctly
    src = build("fs", {"extra": {
        "dir": str(tmp_path),
        "filter_enabled": "true",
        "max_bytes": "1000",
        "supported_exts": [".pdf", ".txt"],
    }})
    (tmp_path / "doc.pdf").write_bytes(b"x" * 5000)   # supported -> kept
    (tmp_path / "blob.bin").write_bytes(b"x" * 5000)   # unsupported & >=1000 -> dropped
    (tmp_path / "note.txt").write_bytes(b"x" * 5000)   # supported -> kept
    keys = set(i.key for i in src.read(None))
    assert keys == {"doc.pdf", "note.txt"}


def test_direct_constructor_defaults():
    src = FsSource({"extra": {"dir": "/x"}})
    assert src.filter_enabled is True
    assert src.max_bytes == 10 * 1000 * 1000
    assert ".pdf" in src.supported_exts
    assert src._keep(".pdf", 10**12) is True   # supported, any size
    assert src._keep(".mov", 20 * 1000 * 1000) is False  # unsupported + large
    assert src._keep(".dat", 100) is True       # unsupported but small
