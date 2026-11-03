# piloop-min-gain

A plugin that adds a [best rule](https://piloop.autoresearch.inc/plugins/rules#best-rules). piloop asks the best
rule whether a confirmed candidate may be the best. This rule calls the built-in rule, `baseline`, and changes one
thing: a candidate whose gain over the baseline is below `min_gain` may not be best.

The guardrails, the sealed split and the reasons stay those of `baseline`. Use the rule when a tiny gain is not worth
a new best, for example on a compressed size. The rule imports only its contract and `baseline`, so it is a pure
plugin.

```bash
uv tool install piloop --with ./examples/plugins/min-gain   # or: uv pip install ./examples/plugins/min-gain
piloop plugins                                              # lists `min_gain  piloop-min-gain 0.1.0`
```

Select it in `piloop.toml`, then `piloop init .`:

```toml
[plugins.best_rule]
provider = "min_gain"
min_gain = 100   # in the primary metric's units: 100 bytes for a size
```
