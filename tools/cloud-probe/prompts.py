"""One explicit, default-No choice before an operation starts."""


class Cancelled(Exception):
    """The user declined before account access or downloads."""


def confirm(question):
    try:
        answer = input(question + ' [y/N] ').strip().casefold()
    except (EOFError, KeyboardInterrupt):
        raise Cancelled from None
    if answer not in ('y', 'yes'):
        raise Cancelled
