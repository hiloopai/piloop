When your run ends, the daemon kills every process that you started, also one in the background, under `nohup` or
`setsid`. Nobody prompts you when a job ends. So:

- Start a long job in the background, and wait for it in steps shorter than the timeout of your command tool. Wait by
  its process id, which `$!` gives: `timeout 540 sh -c 'while kill -0 <pid> 2>/dev/null; do sleep 30; done'`.
- Never wait with `pgrep -f "<pattern>"`: it finds the waiting shell itself, and the wait never ends.
- A job that runs elsewhere (a Modal app, a remote host) outlives your run. Follow its logs or poll its status, and
  stop it when you no longer need it.
- A job that cannot end before your deadline is lost. Run a smaller version that fits, and say in your finding what
  the full run needs.
- Watch each job: is it advancing, at a fair cost, without NaNs, errors or stalls? Kill or fix a bad one at once.
- While a long job runs, post a short `note` on your run now and then: the ETA, the metric, the throughput.
  `target_id` is `(SELECT run_id FROM agent_sessions WHERE id = <your agent session id>)`.
