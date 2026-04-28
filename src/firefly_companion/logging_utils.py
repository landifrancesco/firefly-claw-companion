from __future__ import annotations

import logging
import sys


def configure_logging(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("firefly_companion")
    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    return logger
