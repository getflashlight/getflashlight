# Install Flashlight

## Requirements

- Python 3.12 or later
- Network access only when downloading the package, the sample dataset, or source data
- A writable local directory for the Parquet lake

## Install from PyPI

```bash
pip install getflashlight
flashlight --help
```

The package is named `getflashlight`; the import and command names are `flashlight`.
The short command `fl` is also available.

## Install from source

```bash
git clone https://github.com/ychaparala/getflashlight.git
cd getflashlight
uv sync
uv run flashlight --help
```

## Choose a lake location

By default, Flashlight uses the platform user-data directory. Set `FLASHLIGHT_HOME` when
you want a project-local location, a larger volume, or an explicit location for a service:

```bash
export FLASHLIGHT_HOME="$PWD/.flashlight"
flashlight init
```

`flashlight init` creates the directory layout and starter configuration. It does not
contact a cloud provider or ingest any data.

## Verify the installation

Run the [quickstart](../quickstart.md). It downloads a public sample, writes it to the
lake, and starts the dashboard. If any of these steps fail, see
[Troubleshooting](../troubleshooting/index.md).
