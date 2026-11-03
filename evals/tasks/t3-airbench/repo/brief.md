# CIFAR-10: train to 94% as fast as you can

Make `solution.run` train a network on CIFAR-10 in less time, keeping its accuracy. The starting code is airbench94
with the Muon optimizer (`airbench94_muon.py`, MIT licence in `AIRBENCH_LICENSE`), one of the fastest known: on an
A100 it reaches 94.01% on the test set in about 2.6 s.

## Rules

- `solution.py` defines `build()` and `run(model, seed)`; read its docstring. Edit it and `airbench94_muon.py` freely.
  Do not edit `evaluate.py` or `data/`.
- `run` trains from scratch: nothing it learns may carry over from an earlier call, or from any data other than the
  training images and labels it is given. It returns logits for the test images, whose labels it never sees.
- Use only the Python packages that `evaluate.py` runs with (torch, torchvision, numpy).
- The final version is also trained on all 50,000 training images and scored on CIFAR-10's test set, where its mean
  accuracy over 10 seeds must stay at or above 93.54%, 0.48 points below the starting code's 94.02%. An accuracy
  drop on the held-out part here costs about 2.2 times as much on the test set.

## Measure

Each GPU of this machine runs one evaluation at a time: always go through `/opt/evals/gpu-lock`.

- `/opt/evals/gpu-lock python3 evaluate.py --fast`: 1 seed in a warm process: about 10 s when the code is unchanged,
  30 s after an edit that changes no compiled graph, longer after one that does. Where the warm process is off, it
  runs the 5-seed evaluation instead.
- `/opt/evals/gpu-lock python3 evaluate.py`: 5 seeds from a cold start, about 70 s once the compile cache is warm.

Both print `seconds`, the mean time of `run`, which is the metric to improve (lower is better), and `accuracy` on a
held-out part of the training set. The 5-seed evaluation fails a version whose mean accuracy is below 93.00%, 0.22
points under the starting code's 93.22%, which the starting code passes nearly always. The test set's floor is 2.2
times as far below its own starting code, so a version at this floor holds there. The fast loop only warns below it.
