"""A bound on a call to a platform: ``within``. A call that hangs fails, and never holds the caller.

A provisioner's verbs run on the scaling pass, one at a time: a platform's call that never returns would stop every
later pass. Not every SDK bounds its calls (Modal's do not), so a provider bounds each verb with ``within``.
"""

import threading
from collections.abc import Callable


def within[T](seconds: float, call: Callable[[], T], what: str) -> T:
    """``call()``, on a thread of its own, or ``TimeoutError`` after ``seconds``.

    A call that times out goes on in the background, and nothing reads what it returns.

    Raises:
        TimeoutError: ``call`` did not return in ``seconds``.
    """
    values: list[T] = []
    errors: list[BaseException] = []
    done = threading.Event()

    def run() -> None:
        try:
            values.append(call())
        except BaseException as e:  # handed to the caller, which raises it
            errors.append(e)
        finally:
            done.set()

    threading.Thread(target=run, name=f'within: {what}', daemon=True).start()
    if not done.wait(seconds):
        raise TimeoutError(f'{what} did not answer in {seconds:g} s')
    if errors:
        raise errors[0]
    return values[0]
