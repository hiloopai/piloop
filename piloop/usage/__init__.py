"""Reading the token usage of agents: why it failed, and how a count is shown. The ``Usage`` is the harnesses'."""


class UsageError(RuntimeError):
    """The usage could not be read.

    The message says why: no session id, no file, no usage in the file, or a harness that is no longer configured.
    """


def human(tokens: int) -> str:
    """A short form of a token count: ``1.2M``, ``340k``, ``987``."""
    if tokens >= 1_000_000:
        return f'{tokens / 1_000_000:.1f}M'
    if tokens >= 1_000:
        return f'{tokens / 1_000:.0f}k'
    return str(tokens)
