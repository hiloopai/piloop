"""The training run that evaluate.py times: edit this file and ``airbench94_muon.py`` freely.

evaluate.py calls ``build()`` once per process, untimed, then ``run(model, seed)`` once per seed, timed. Both run in a
directory whose ``cifar10/train.pt`` and ``cifar10/test.pt`` hold the split, as ``airbench94_muon.py`` reads them. The
test labels there are zeros: ``run`` returns logits for the test images, and evaluate.py checks them.
"""

import airbench94_muon as ab
import torch


def build() -> torch.nn.Module:
    """The compiled network, warmed up on the training images with random labels, as upstream does."""
    model = ab.CifarNet().cuda().to(memory_format=torch.channels_last)
    model.compile(mode='max-autotune')
    ab.main('warmup', model)
    return model


def run(model: torch.nn.Module, seed: int) -> torch.Tensor:
    """Train ``model`` from scratch on ``cifar10/train.pt``, then return its logits for ``cifar10/test.pt``."""
    torch.manual_seed(seed)
    logits: list[torch.Tensor] = []

    def evaluate(model: torch.nn.Module, loader: ab.CifarLoader, tta_level: int = 0) -> float:
        if tta_level:  # the final evaluation, with test-time augmentation; the per-epoch ones are skipped
            logits.append(ab.infer(model, loader, tta_level))
        return 0.0

    ab.evaluate = evaluate
    ab.main(seed, model)
    return logits[-1]
