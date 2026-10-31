"""Time ``solution.run`` on CIFAR-10 and check its accuracy: print ``seconds`` and ``accuracy`` as one JSON line.

Each seed is one timed call of ``solution.run(model, seed)``: it trains on ``cifar10/train.pt`` and returns logits for
the images of ``cifar10/test.pt``, whose labels it never sees. ``seconds`` is the mean wall time of those calls, the
GPU synchronised; ``solution.build``, which compiles and warms the model, is not timed. ``accuracy`` is the mean over
seeds. Below the floor, the run fails.

- ``python evaluate.py``: the slow dev loop, 5 seeds from a cold start, training on ``data/train.pt`` and testing on
  ``data/val.pt``.
- ``python evaluate.py --fast``: the fast dev loop, 1 seed in a warm process that keeps one compiled model per version
  of the code. It starts on first use, one per GPU, and ends after any failure.
- ``python evaluate.py --train A.pt [B.pt ...] --test T.pt --seeds N --floor F``: any split, as the sealed scorers use.

Run it through ``/opt/evals/gpu-lock`` so that each evaluation has a GPU to itself. It runs on its own Python, with
torch, wherever it is started from.
"""

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent
VENV = Path('/opt/evals/venv/t3-airbench/bin/python')
SEEDS = 5
FLOOR = 0.9301
"""The accuracy below which a version fails, over 5 seeds: the starting code's mean on ``data/val.pt``, 0.9325, less 3
standard errors of a 5-seed mean (one seed's standard deviation is 0.0017)."""
FAST_FLOOR = 0.9273
"""The same for one seed: the mean less 3 standard deviations."""
SERVE_WAIT = 600
"""Seconds that the fast loop waits for its warm process to start, then for one evaluation."""

if Path(sys.executable).resolve() != VENV.resolve() and VENV.exists():
    os.execv(VENV, [str(VENV), __file__, *sys.argv[1:]])

os.environ.setdefault('TORCHINDUCTOR_FX_GRAPH_CACHE', '1')
"""Compiled graphs are cached on disk, so a version that changes no graph compiles from the cache."""
import torch  # noqa: E402  # after the re-exec: only the task's Python has it


def load(files: list[Path]) -> tuple[torch.Tensor, torch.Tensor]:
    """The images and labels of ``files``, one after the other."""
    parts = [torch.load(f) for f in files]
    return torch.cat([p['images'] for p in parts]), torch.cat([p['labels'] for p in parts])


class Split:
    """A training set and a test set, written where ``solution`` reads them: ``cifar10/`` in a directory of its own."""

    def __init__(self, train: list[Path], test: Path) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix='cifar-'))
        (self.dir / 'cifar10').mkdir()
        images, labels = load(train)
        torch.save({'images': images, 'labels': labels, 'classes': []}, self.dir / 'cifar10' / 'train.pt')
        test_images, self.labels = load([test])
        hidden = torch.zeros_like(self.labels)
        torch.save({'images': test_images, 'labels': hidden, 'classes': []}, self.dir / 'cifar10' / 'test.pt')


def code(tree: Path) -> str:
    """A hash of the Python files at the root of ``tree``: one compiled model per version of the code."""
    digest = hashlib.sha256()
    for f in sorted(tree.glob('*.py')):
        digest.update(f.name.encode() + f.read_bytes())
    return digest.hexdigest()


def solution(tree: Path, tag: str) -> ModuleType:
    """``tree/solution.py``, imported under a name of its own, with ``tree`` first on the path."""
    sys.path.insert(0, str(tree))
    for name in [n for n in sys.modules if n in {'solution', 'airbench94_muon'}]:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(f'solution_{tag}', tree / 'solution.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.path.remove(str(tree))
    return module


@contextlib.contextmanager
def inside(directory: Path):
    """Run in ``directory``, with the solution's prints kept out of the result."""
    cwd = os.getcwd()
    os.chdir(directory)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            yield
    finally:
        os.chdir(cwd)


def timed(module: ModuleType, model: object, split: Split, seeds: list[int]) -> dict[str, float]:
    """Seconds and accuracy, the means over ``seeds``."""
    seconds, accuracy = [], []
    for seed in seeds:
        with inside(split.dir):
            torch.cuda.synchronize()
            started = time.perf_counter()
            logits = module.run(model, seed)
            torch.cuda.synchronize()
            seconds.append(time.perf_counter() - started)
        accuracy.append((logits.argmax(1).cpu() == split.labels).float().mean().item())
    return {'seconds': round(statistics.fmean(seconds), 4), 'accuracy': round(statistics.fmean(accuracy), 5)}


def cold(tree: Path, split: Split, seeds: int) -> dict[str, float]:
    """Build the model and time ``seeds`` runs, in this process."""
    module = solution(tree, 'cold')
    with inside(split.dir):
        model = module.build()
    return timed(module, model, split, list(range(seeds)))


def serve(path: str) -> None:
    """The warm process of one GPU: one compiled model per version of the code, until any failure."""
    split = Split([ROOT / 'data' / 'train.pt'], ROOT / 'data' / 'val.pt')
    models: dict[str, tuple[ModuleType, object]] = {}
    server = socket.socket(socket.AF_UNIX)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    server.bind(path)
    server.listen()
    while True:
        conn, _ = server.accept()
        request = json.loads(conn.makefile().readline())
        tree = Path(request['tree'])
        try:
            key = code(tree)
            if key not in models:
                models.clear()
                torch.cuda.empty_cache()
                module = solution(tree, key[:12])
                with inside(split.dir):
                    models[key] = (module, module.build())
            module, model = models[key]
            reply: dict[str, object] = {'metrics': timed(module, model, split, [request['seed']])}
        except BaseException:
            conn.sendall((json.dumps({'error': traceback.format_exc()[-3000:]}) + '\n').encode())
            conn.close()
            os.unlink(path)
            raise
        conn.sendall((json.dumps(reply) + '\n').encode())
        conn.close()


def connect(path: str) -> socket.socket | None:
    """A connection to the warm process at ``path``, or None when none listens there."""
    conn = socket.socket(socket.AF_UNIX)
    try:
        conn.connect(path)
    except OSError:
        conn.close()
        return None
    return conn


def fast() -> dict[str, float]:
    """One seed in the warm process of this GPU, which starts on first use."""
    path = f'/tmp/airbench-dev-{os.environ.get("CUDA_VISIBLE_DEVICES", "0")}.sock'
    conn = connect(path)
    if conn is None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)  # a process that was killed left it
        with open(f'{path}.log', 'a') as log:
            subprocess.Popen(
                [sys.executable, __file__, '--serve', path], stdout=log, stderr=log, start_new_session=True
            )
        deadline = time.monotonic() + SERVE_WAIT
        while (conn := connect(path)) is None:
            if time.monotonic() > deadline:
                raise RuntimeError(f'the warm process did not start: see {path}.log')
            time.sleep(1)
    conn.settimeout(SERVE_WAIT)
    conn.sendall((json.dumps({'tree': str(Path.cwd()), 'seed': 0}) + '\n').encode())
    answer = json.loads(conn.makefile().readline() or '{"error": "the warm process ended"}')
    if 'error' in answer:
        raise RuntimeError(answer['error'])
    return answer['metrics']


def report(metrics: dict[str, float], floor: float) -> int:
    """Print the metrics as one JSON line; exit 1 below the accuracy floor."""
    if metrics['accuracy'] < floor:
        print(f'accuracy {metrics["accuracy"]:.4f} is below {floor:.4f}', file=sys.stderr)
        return 1
    line = json.dumps({'metrics': metrics})
    if result := os.environ.get('PILOOP_RESULT_FILE'):
        with open(result, 'a') as f:
            f.write(line + '\n')
    else:
        print(line)
    return 0


def main() -> int:
    """Parse the arguments and evaluate."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--fast', action='store_true')
    parser.add_argument('--serve')
    parser.add_argument('--train', nargs='+', type=Path)
    parser.add_argument('--test', type=Path)
    parser.add_argument('--seeds', type=int, default=SEEDS)
    parser.add_argument('--floor', type=float, default=FLOOR)
    args = parser.parse_args()
    if args.serve:
        serve(args.serve)
        return 0
    try:
        if args.fast and os.environ.get('EVALS_DEV_LOOP') != 'slow':  # a slow-loop arm turns the warm process off
            return report(fast(), FAST_FLOOR if args.floor == FLOOR else args.floor)
        split = Split(args.train or [ROOT / 'data' / 'train.pt'], args.test or ROOT / 'data' / 'val.pt')
        return report(cold(Path.cwd(), split, args.seeds), args.floor)
    except Exception as e:  # the reason of a failed run, on stderr: piloop records a crash
        print(f'{type(e).__name__}: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
