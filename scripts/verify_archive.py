from __future__ import annotations

import argparse
import hashlib
import os
import stat as stat_module
from pathlib import Path

EXCLUDED_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".lock", ".tmp", ".swp", ".swo", ".sock", ".sqlite-wal", ".sqlite-shm")


def excluded(relative: Path) -> bool:
    name = relative.name
    temporary_snapshot = name.startswith(".hh_vacancies_") and ".json." in name
    return (any(part in EXCLUDED_PARTS for part in relative.parts) or name.endswith(EXCLUDED_SUFFIXES)
            or temporary_snapshot)


def manifest(root: Path) -> dict[str, tuple[str, int, str]]:
    result: dict[str, tuple[str, int, str]] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        dirs[:] = [name for name in dirs if not excluded(current_path.joinpath(name).relative_to(root))]
        for name in files:
            path = current_path / name
            relative = path.relative_to(root)
            if excluded(relative):
                continue
            stat_info = path.lstat()
            if stat_module.S_ISLNK(stat_info.st_mode):
                result[str(relative)] = ("symlink", stat_info.st_size, os.readlink(path))
                continue
            if not stat_module.S_ISREG(stat_info.st_mode):
                # Sockets, fifos and device nodes are deliberately not read or followed.
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            result[str(relative)] = ("file", stat_info.st_size, digest)
    return result


def compare(source: Path, archive: Path) -> tuple[list[str], list[str], list[str]]:
    source_manifest = manifest(source)
    archive_manifest = manifest(archive)
    missing = sorted(set(source_manifest) - set(archive_manifest))
    extra = sorted(set(archive_manifest) - set(source_manifest))
    mismatched = sorted(path for path in set(source_manifest) & set(archive_manifest)
                        if source_manifest[path] != archive_manifest[path])
    return missing, extra, mismatched


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare the private archive with its source by path, size and SHA-256")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    missing, extra, mismatched = compare(args.source, args.archive)
    lines = [
        f"source={args.source}", f"archive={args.archive}",
        "excluded=.git,.venv,__pycache__,.pytest_cache,.ruff_cache,node_modules,temporary suffixes,snapshot temps",
        f"missing={len(missing)}", f"extra={len(extra)}", f"mismatched={len(mismatched)}",
    ]
    if missing:
        lines.append("missing_paths=" + ",".join(missing))
    if extra:
        lines.append("extra_paths=" + ",".join(extra))
    if mismatched:
        lines.append("mismatched_paths=" + ",".join(mismatched))
    output = "\n".join(lines) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if not missing and not mismatched else 1


if __name__ == "__main__":
    raise SystemExit(main())
