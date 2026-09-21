# Packaging ApplyPilot as a Debian `.deb`

`build-deb.sh` produces a self-contained Debian package. It bundles a Python
virtual environment (with ApplyPilot installed) under `/opt/applypilot` and
installs a launcher at `/usr/local/bin/applypilot`.

## Build

From the repository root:

```sh
bash packaging/build-deb.sh
```

The resulting file is written to the repository root as
`applypilot_<version>_amd64.deb`.

### Options

Both options are passed as environment variables:

- `VERSION` — override the package version. By default the version is read
  from `[project].version` in `pyproject.toml`.

  ```sh
  VERSION=1.2.3 bash packaging/build-deb.sh
  ```

- `WITH_BROWSER=1` — also install the optional `browser` extra (Playwright)
  into the bundled virtual environment.

  ```sh
  WITH_BROWSER=1 bash packaging/build-deb.sh
  ```

They can be combined:

```sh
VERSION=1.2.3 WITH_BROWSER=1 bash packaging/build-deb.sh
```

## Install

```sh
sudo dpkg -i applypilot_*.deb
```

If `dpkg` reports missing dependencies, resolve them with:

```sh
sudo apt-get -f install
```

## Run

```sh
applypilot --help
applypilot admin
```

The launcher simply runs `python -m applypilot` inside the bundled venv, so any
subcommand works the same as running the tool from source.

## Uninstall

```sh
sudo dpkg -r applypilot
```

## Limitations

- **Network is required at build time.** `build-deb.sh` runs `pip install`
  inside the bundled venv, which downloads dependencies from PyPI.
- **The venv is tied to a specific Python version.** The bundled environment is
  built against the `python3` used at build time (3.12+). The target machine
  must provide a compatible `python3` (declared as `Depends: python3 (>= 3.12)`).
  Building on a different minor version than the target can break the venv.
- **Playwright browsers are not bundled.** Even with `WITH_BROWSER=1`, only the
  Playwright Python package is installed. The user installs the browser binaries
  separately after installation (for example
  `/opt/applypilot/venv/bin/python -m playwright install`).
- **HH.ru sign-in is configured by the user.** Authentication and any HH.ru
  login are set up by the user after installation; nothing is baked into the
  package.
- **Private data is not included.** The repository's `private/` directory and
  any other personal data are never copied into the package.
