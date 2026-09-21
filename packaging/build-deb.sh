#!/usr/bin/env bash
#
# build-deb.sh — build a self-contained Debian package for ApplyPilot.
#
# The resulting .deb bundles a Python virtual environment with the
# application installed under /opt/applypilot, plus a small launcher
# at /usr/local/bin/applypilot.
#
# Usage:
#   bash packaging/build-deb.sh
#   VERSION=1.2.3 bash packaging/build-deb.sh
#   WITH_BROWSER=1 bash packaging/build-deb.sh
#
# Environment variables:
#   VERSION       Override the package version (default: [project].version
#                 from pyproject.toml).
#   WITH_BROWSER  If set to 1, install the optional "browser" extra
#                 (Playwright) into the bundled venv.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve paths.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYPROJECT="${REPO_ROOT}/pyproject.toml"

PACKAGE="applypilot"
ARCH="amd64"
MAINTAINER="ApplyPilot Maintainers <logan200159@gmail.com>"

# ---------------------------------------------------------------------------
# Preflight checks.
# ---------------------------------------------------------------------------
echo "==> Checking build prerequisites"

if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "ERROR: dpkg-deb not found. Install it with: sudo apt-get install dpkg-dev" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.12 or newer." >&2
  exit 1
fi

if [ ! -f "${PYPROJECT}" ]; then
  echo "ERROR: pyproject.toml not found at ${PYPROJECT}" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Determine the version.
# ---------------------------------------------------------------------------
if [ -n "${VERSION:-}" ]; then
  version="${VERSION}"
  echo "==> Using version from VERSION env: ${version}"
else
  version="$(python3 - "${PYPROJECT}" <<'PYEOF'
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

path = sys.argv[1]
if tomllib is not None:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    print(data["project"]["version"])
else:
    # Minimal fallback parser: find version under [project].
    in_project = False
    for line in open(path, encoding="utf-8"):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == "[project]"
            continue
        if in_project and stripped.startswith("version"):
            _, _, rhs = stripped.partition("=")
            print(rhs.strip().strip('"').strip("'"))
            break
PYEOF
)"
  if [ -z "${version}" ]; then
    echo "ERROR: could not determine version from pyproject.toml" >&2
    exit 1
  fi
  echo "==> Detected version from pyproject.toml: ${version}"
fi

# ---------------------------------------------------------------------------
# Prepare the build layout in a temporary directory.
# ---------------------------------------------------------------------------
BUILD_ROOT="$(mktemp -d)"
cleanup() {
  rm -rf "${BUILD_ROOT}"
}
trap cleanup EXIT

STAGE="${BUILD_ROOT}/${PACKAGE}_${version}_${ARCH}"
echo "==> Preparing package layout in ${STAGE}"

mkdir -p "${STAGE}/opt/applypilot"
mkdir -p "${STAGE}/usr/local/bin"
mkdir -p "${STAGE}/DEBIAN"

# ---------------------------------------------------------------------------
# Create the bundled virtual environment and install the application.
# ---------------------------------------------------------------------------
VENV_DIR="${STAGE}/opt/applypilot/venv"
echo "==> Creating virtual environment at ${VENV_DIR}"
python3 -m venv "${VENV_DIR}"

echo "==> Upgrading pip inside the venv"
"${VENV_DIR}/bin/pip" install --upgrade pip >/dev/null

if [ "${WITH_BROWSER:-0}" = "1" ]; then
  echo "==> Installing applypilot with the 'browser' extra (this needs network access)"
  "${VENV_DIR}/bin/pip" install "${REPO_ROOT}[browser]"
else
  echo "==> Installing applypilot (this needs network access)"
  "${VENV_DIR}/bin/pip" install "${REPO_ROOT}"
fi

# ---------------------------------------------------------------------------
# Install the launcher.
# ---------------------------------------------------------------------------
LAUNCHER="${STAGE}/usr/local/bin/applypilot"
echo "==> Writing launcher to /usr/local/bin/applypilot"
cat > "${LAUNCHER}" <<'LAUNCHEOF'
#!/bin/sh
exec /opt/applypilot/venv/bin/python -m applypilot "$@"
LAUNCHEOF
chmod 755 "${LAUNCHER}"

# ---------------------------------------------------------------------------
# Write the DEBIAN/control file.
# ---------------------------------------------------------------------------
echo "==> Writing DEBIAN/control"
cat > "${STAGE}/DEBIAN/control" <<CONTROLEOF
Package: ${PACKAGE}
Version: ${version}
Section: utils
Priority: optional
Architecture: ${ARCH}
Maintainer: ${MAINTAINER}
Depends: python3 (>= 3.12)
Description: Local, explainable HH.ru job-search and application workflow
 ApplyPilot is a local command-line tool that helps search for jobs on
 HH.ru, score and review vacancies, draft cover letters and manage the
 application workflow in an explainable way.
 .
 This package bundles a self-contained Python virtual environment under
 /opt/applypilot and exposes the "applypilot" command via
 /usr/local/bin. Playwright browsers and HH.ru sign-in are configured by
 the user after installation.
CONTROLEOF

# ---------------------------------------------------------------------------
# Build the .deb.
# ---------------------------------------------------------------------------
OUTPUT="${REPO_ROOT}/${PACKAGE}_${version}_${ARCH}.deb"
echo "==> Building package with dpkg-deb"
dpkg-deb --build --root-owner-group "${STAGE}" "${OUTPUT}"

echo
echo "==> Done. Package written to:"
echo "    ${OUTPUT}"
echo
echo "Install it with:"
echo "    sudo dpkg -i ${OUTPUT}"
