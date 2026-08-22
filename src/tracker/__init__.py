"""Amazon Price Tracker - GitHub Actions based price tracking with ntfy."""
from .config import Settings, load_products, load_settings
from .database import Database
from .tracker import Tracker

__version__ = "1.0.0"

__all__ = [
    "Settings",
    "load_products",
    "load_settings",
    "Database",
    "Tracker",
    "__version__",
]
