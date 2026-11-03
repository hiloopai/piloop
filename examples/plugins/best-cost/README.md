# piloop-best-cost

A plugin that adds a [stop rule](https://piloop.autoresearch.inc/plugins/rules#stop-rules). It
compares the fact that `spend` names, since the latest new best, with its value per new best so far. Past `escalate_at`
times, it asks a human once for each new best, and the campaign goes on. Past `stop_at` times, it stops the campaign. It is the
built-in rule `limits` with these two lines, so `max_hours`, `hold_at` and the other limits still apply. It needs a
source in `[plugins.fact_source] use` that reads the fact that `spend` names, such as a `command` source.

```bash
uv tool install piloop --with ./examples/plugins/best-cost   # or: uv pip install ./examples/plugins/best-cost
piloop plugins                                               # lists `best_cost  piloop-best-cost 0.1.0`
```

Select it in `piloop.toml`, then `piloop init .`:

```toml
[plugins.fact_source]
use = ["campaign", "optimization", "billing"]

[plugins.fact_source.billing]
provider = "command"
command = ["uv", "run", "scripts/spend.py"]   # reports {"metrics": {"billing_usd": <usd>}}
metrics = ["billing_usd"]

[plugins.stop_rule]
provider = "best_cost"
spend = "billing_usd"
escalate_at = 2
stop_at = 4
hold_at = { billing_usd = 5000 }
```
