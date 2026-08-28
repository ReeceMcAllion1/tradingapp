"""Strategy registry.

Importing the modules below is what registers them, so a new strategy becomes
available to the CLI as soon as it is imported here.
"""

from .base import Context, Strategy, available, build, register
from .benchmark import BuyAndHold
from .mean_reversion import MeanReversion
from .micro_scalp import MicroScalp
from .never_lose import NeverLose
from .slow_trend import SlowTrend
from .trend import EmaCross

__all__ = [
    "Context",
    "Strategy",
    "available",
    "build",
    "register",
    "BuyAndHold",
    "EmaCross",
    "MeanReversion",
    "MicroScalp",
    "NeverLose",
    "SlowTrend",
]
