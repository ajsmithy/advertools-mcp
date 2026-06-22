"""advertools-mcp: an MCP server wrapping the advertools SEO toolkit."""

from .config import get_settings
from .server import build_server

__version__ = "0.1.0"
__all__ = ["build_server", "get_settings", "__version__"]
