import logging

logger = logging.getLogger("mimchine")
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_handler)
logger.propagate = False


def configure_logging(verbose_count: int, quiet: bool) -> None:
    if quiet:
        logger.setLevel(logging.WARNING)
        return

    if verbose_count >= 2:
        logger.setLevel(logging.DEBUG)
        return

    if verbose_count >= 1:
        logger.setLevel(logging.INFO)
        return

    logger.setLevel(logging.INFO)
