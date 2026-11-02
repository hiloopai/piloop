"""Log each run of a task's ``evaluate.py`` in a run's sandbox to ``$EVALS_EVAL_LOG``, one JSON line when it exits.

Python imports this module at start-up from ``PYTHONPATH``, so every evaluation counts, however an agent starts it:
by hand, from a script of its own, or through piloop. A line has when it started (``at``, Unix seconds), how long it
took, its arguments, and whether piloop ran it (piloop gives only the commands it reads ``PILOOP_RESULT_FILE``).
A warm server (``--serve``) is no evaluation, and a process killed by a signal writes no line.
"""

import atexit
import json
import os
import sys
import time

_LOG = os.environ.get('EVALS_EVAL_LOG')

if _LOG and sys.argv and os.path.basename(sys.argv[0]) == 'evaluate.py' and '--serve' not in sys.argv:
    _started = time.time()

    def _log(path: str) -> None:
        line = {
            'at': _started,
            'seconds': time.time() - _started,
            'argv': sys.argv[1:],
            'piloop': 'PILOOP_RESULT_FILE' in os.environ,
        }
        with open(path, 'a') as f:
            f.write(json.dumps(line) + '\n')

    atexit.register(_log, _LOG)
