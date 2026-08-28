"""Strategy registry.

Importing the modules below is what registers them, so a new strategy becomes
available to the CLI as soon as it is imported here.
"""

from .base import Context, Strategy, available, build, register
from .mean_reversion import MeanReversion
from .micro_scalp import MicroScalp
from .trend import EmaCross

__all__ = [
    "Context",
    "Strategy",
    "available",
    "build",
    "register",
    "EmaCross",
    "MeanReversion",
    "MicroScalp",
]
