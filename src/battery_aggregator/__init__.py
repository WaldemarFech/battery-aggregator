"""battery-aggregator: device-independent core for aggregating parallel LiFePO4 packs."""
from .config import Config, PackConfig
from .core import Core
from .fault import SafeCore
from .model import BankMode, PackSnapshot, PackState
from .outputs import BankOutputs

__version__ = "0.4.2"

__all__ = ["Config", "PackConfig", "Core", "SafeCore", "BankMode", "PackSnapshot", "PackState",
           "BankOutputs", "__version__"]
