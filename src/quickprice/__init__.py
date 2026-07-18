"""QuickPrice private market-data service."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("quickprice")
except PackageNotFoundError:  # pragma: no cover - editable source without metadata
    __version__ = "1.5.0"

__all__ = ["__version__"]
