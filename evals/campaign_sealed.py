"""A piloop campaign's ``[evaluator] sealed`` command: the task's sealed scorer on the campaign's own sealed split.

piloop runs it at the root of a checkout of the version. It scores a copy of that checkout with the task's own
``evaluate.py``, against the task's starting repo, on the split in ``<sealed dir>``, which lives in the campaign's home
and not in any agent's checkout. That split is not the holdout that the runner scores every arm on afterwards: a keep
chosen on the holdout would be judged on its own selection. It writes one result line, as piloop's evaluate command
does. piloop compares it with the primary metric, so its score is in the primary's ``direction``: the scorer's raw
value when it has one number, else its normalised score, which is higher when better.

Usage: ``python3 campaign_sealed.py <task dir> <sealed dir> <direction>``, with only the standard library.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main(task: Path, sealed: Path, direction: str) -> dict[str, object]:
    """Score the version in the current directory."""
    with tempfile.TemporaryDirectory() as tmp:
        tree, out = Path(tmp) / 'tree', Path(tmp) / 'sealed.json'
        shutil.copytree(Path.cwd(), tree, ignore=shutil.ignore_patterns('.git', '.venv', 'data'))
        shutil.copy(task / 'repo' / 'evaluate.py', tree / 'evaluate.py')
        argv = [sys.executable, str(task / 'sealed.py'), str(tree), str(task / 'repo'), str(sealed), str(out)]
        done = subprocess.run(argv, capture_output=True, text=True)
        if done.returncode != 0:
            return {'status': 'infra_error', 'feedback': done.stderr.strip()[-500:]}
        verdict = json.loads(out.read_text())
    if verdict['status'] != 'ok':
        return {'status': 'crash', 'feedback': str(verdict['detail'])[-500:]}
    raw = verdict.get('raw')
    if isinstance(raw, int | float):
        return {'status': 'ok', 'score': raw, 'direction': direction}
    if direction != 'higher':
        return {
            'status': 'infra_error',
            'feedback': f'a normalised sealed score is higher when better, not {direction}',
        }
    return {'status': 'ok', 'score': verdict['score'], 'direction': direction}


if __name__ == '__main__':
    line = json.dumps(main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]))
    with open(os.environ['PILOOP_RESULT_FILE'], 'a') as f:
        f.write(line + '\n')
