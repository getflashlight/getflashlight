"""Flashlight — FOCUS-based multi-cloud spend-visualization platform."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("getflashlight")
except PackageNotFoundError:
    # Keep source-tree imports usable before the package has been installed.
    __version__ = "0+unknown"
