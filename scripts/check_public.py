from __future__ import annotations

import re
import subprocess
import tarfile
import zipfile
from pathlib import Path

FORBIDDEN_PATHS = ("private/", ".hh_session", "session.json")
SECRET_PATTERNS = (
    re.compile(r"OPENROUTER_API_KEY\s*=\s*['\"]?sk-or-v1-[A-Za-z0-9_-]+"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
)


def forbidden_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if any(token in path for token in FORBIDDEN_PATHS)
            or (path.startswith(".env") and path != ".env.example")]


def content_has_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def check_history(repo: str) -> list[str]:
    commits = subprocess.run(["git", "-c", f"safe.directory={repo}", "rev-list", "--all"],
                             check=True, text=True, capture_output=True).stdout.splitlines()
    errors: list[str] = []
    for commit in commits:
        paths = subprocess.run(["git", "-c", f"safe.directory={repo}", "ls-tree", "-r", "--name-only", commit],
                               check=True, text=True, capture_output=True).stdout.splitlines()
        for path in forbidden_paths(paths):
            errors.append(f"{commit[:12]}:{path}")
        for pattern in SECRET_PATTERNS:
            found = subprocess.run(["git", "-c", f"safe.directory={repo}", "grep", "-I", "-l", "-E",
                                    pattern.pattern, commit, "--"], check=False, text=True,
                                   capture_output=True).stdout.splitlines()
            errors.extend(f"{commit[:12]}:{path}" for path in found)
    return sorted(set(errors))


def check_artifacts() -> list[str]:
    errors: list[str] = []
    for path in sorted(Path("dist").glob("*")) if Path("dist").exists() else []:
        try:
            if path.suffix == ".whl":
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
                    if forbidden_paths(names):
                        errors.extend(f"{path}:{name}" for name in forbidden_paths(names))
                    for name in names:
                        if not name.endswith((".py", ".toml", ".md", ".txt")):
                            continue
                        if content_has_secret(archive.read(name).decode("utf-8", errors="ignore")):
                            errors.append(f"{path}:{name}")
            elif path.name.endswith((".tar.gz", ".tar")):
                with tarfile.open(path) as archive:
                    names = archive.getnames()
                    if forbidden_paths(names):
                        errors.extend(f"{path}:{name}" for name in forbidden_paths(names))
                    for member in archive.getmembers():
                        if not member.isfile() or not member.name.endswith((".py", ".toml", ".md", ".txt")):
                            continue
                        handle = archive.extractfile(member)
                        if handle and content_has_secret(handle.read().decode("utf-8", errors="ignore")):
                            errors.append(f"{path}:{member.name}")
        except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
            errors.append(f"{path}:unreadable-artifact:{type(exc).__name__}")
    return errors


def main() -> int:
    repo = str(Path.cwd().resolve())
    result = subprocess.run(["git", "-c", f"safe.directory={repo}", "ls-files"],
                            check=True, text=True, capture_output=True)
    paths = result.stdout.splitlines()
    bad_paths = forbidden_paths(paths)
    if bad_paths:
        print("forbidden tracked paths:")
        print("\n".join(bad_paths))
        return 1
    bad_content: list[str] = []
    for path in paths:
        file_path = Path(path)
        if not file_path.is_file() or file_path.stat().st_size > 2_000_000:
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if content_has_secret(text):
            bad_content.append(path)
    if bad_content:
        print("possible secrets in tracked files:")
        print("\n".join(bad_content))
        return 1
    history_errors = check_history(repo)
    artifact_errors = check_artifacts()
    if history_errors or artifact_errors:
        print("public history/artifact check failed:")
        print("\n".join(history_errors + artifact_errors))
        return 1
    print(f"public-check-ok: {len(paths)} tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
