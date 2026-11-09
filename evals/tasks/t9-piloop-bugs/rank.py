"""The piloop-bugs task's ``evaluate`` for a piloop arm: the share of the campaign split's held-out tests that pass.

A piloop arm's pool ranks its candidates on the primary metric of ``evaluate``, and on a suite that stays green every
correct candidate would tie with the baseline, so no best would ever move. It ranks on the campaign's own sealed split
instead, which is piloop's evaluator as t5's sealed split is, and which shares no test with the holdout. Run as
``python3 rank.py <sealed dir>`` at the root of a checkout, as piloop runs ``evaluate``, on an evaluator's machine,
which holds the split (``evals.drive.SEALED``); an agent's machine has none, and a plain arm runs ``evaluate.py`` alone.
It writes one result line to ``$PILOOP_RESULT_FILE``: ``{"status": "ok", "score": <share fixed>, "direction": "higher",
"metrics": {"seconds": ...}}``, or the scorer's failure as ``campaign_sealed.py`` words it.
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

TASK = Path(__file__).resolve().parent


def main(sealed: Path) -> dict[str, object]:
    """Score the version in the current directory on the campaign's split in ``sealed``."""
    t0 = time.monotonic()
    spec = importlib.util.spec_from_file_location('campaign_sealed', TASK.parent.parent / 'campaign_sealed.py')
    assert spec is not None and spec.loader is not None
    campaign_sealed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(campaign_sealed)
    verdict = campaign_sealed.main(TASK, sealed, 'higher')
    if verdict['status'] != 'ok':
        return verdict
    seconds = round(time.monotonic() - t0, 1)
    return {'status': 'ok', 'score': verdict['score'], 'direction': 'higher', 'metrics': {'seconds': seconds}}


if __name__ == '__main__':
    line = json.dumps(main(Path(sys.argv[1])))
    with open(os.environ['PILOOP_RESULT_FILE'], 'a') as f:
        f.write(line + '\n')
