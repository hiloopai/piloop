## Cost track

Decrease `cost_seconds`. Do not change the predictions, or change them as little as possible. Time `solve.py` first
(`python -X importtime solve.py …`, `cProfile`), then remove what costs the most: heavy imports, repeated work, a model
that is too large. The best of the cost track may lose at most the accuracy margin against the baseline, so it can
change only a few predictions.
