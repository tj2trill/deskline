"""edgelab: brute-force trading-edge search with an out-of-sample random-chart gauntlet."""
from .pipeline import Config, run  # noqa: F401
from .signals import FAMILIES, Strategy, generate  # noqa: F401
