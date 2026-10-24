"""麦麦三路比价侦探 · 基于麦当劳 MCP 的渠道到手价对比 Skill。

- 版本：1.0.0
- 参赛：麦当劳程序员创意开发大赛（2026 1024 程序员节）
- Token 约定：只读环境变量 ``MCD_MCP_TOKEN``，代码内不含任何真实凭证。
"""

from .client import McpClient, McpError, load_token, load_url
from .compare import Comparator, Comparison
from .model import CartItem, Channel, ChannelQuote, Coupon, Store, parse_items, yuan
from .tools import ReplayCassette, ToolCatalog

__version__ = "1.0.0"
__all__ = [
    "McpClient", "McpError", "load_token", "load_url",
    "ToolCatalog", "ReplayCassette", "Comparator", "Comparison",
    "Channel", "ChannelQuote", "CartItem", "Coupon", "Store", "parse_items", "yuan",
    "__version__",
]
