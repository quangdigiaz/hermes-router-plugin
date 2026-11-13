"""Directory-plugin entry point loaded by ``hermes plugins install``.

Same shape as the first-party tinyfish plugin: a directory install imports this
file (relative import available), a pip install imports the packaged module
directly (absolute import only).
"""

from __future__ import annotations

try:
    from .hermes_router_hermes import __version__, register
except ImportError:  # pip installs import the packaged module directly
    from hermes_router_hermes import __version__, register  # type: ignore

__all__ = ["__version__", "register"]
