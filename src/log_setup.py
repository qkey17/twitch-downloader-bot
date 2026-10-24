import logging
from .config import LOG_LEVEL, LOG_FORMAT


def setup_logging():
    logging.basicConfig(
        level=LOG_LEVEL.upper(),
        format=LOG_FORMAT,
    )
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
