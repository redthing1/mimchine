from redlog import Level, get_logger, set_level

logger = get_logger("mimchine")


def configure_logging(verbose_count: int, quiet: bool) -> None:
    if quiet:
        set_level(Level.WARN)
        return

    if verbose_count >= 2:
        set_level(Level.DEBUG)
        return

    if verbose_count == 1:
        set_level(Level.VERBOSE)
        return

    set_level(Level.INFO)
