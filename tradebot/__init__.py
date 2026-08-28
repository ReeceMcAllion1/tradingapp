"""tradebot - an automated trading system that charges itself honest costs.

Start here:

    python -m tradebot demo

No part of this package can promise a profit. Read the README before trading.
"""

__version__ = "1.0.0"

from .costs import CostModel
from .risk import RiskLimits
from .types import Candle, Decision, Fill, Side, Trade

__all__ = ["CostModel", "RiskLimits", "Candle", "Decision", "Fill", "Side", "Trade", "__version__"]
