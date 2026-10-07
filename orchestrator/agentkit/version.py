"""Show the source version and expose stale installed package metadata."""
from importlib import metadata

from . import __version__


def report() -> str:
    result = f"agentkit {__version__}"
    try:
        installed = metadata.version("agentkit-orchestrator")
    except metadata.PackageNotFoundError:
        return result + " (source checkout; package metadata unavailable)"
    if installed != __version__:
        result += f" (installed metadata {installed}; reinstall this checkout)"
    return result
