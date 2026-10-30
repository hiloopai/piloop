## This sweep: an incident

Find out why each anomaly under "Incidents to investigate" happened. Use the rows and their `events`, the agent's
transcript and brief, its session (`piloop cas get <agent_sessions.session_file>`), the process table and the
campaign's versions.

Classify each root cause:

- **harness bug:** piloop did the wrong thing. It is never "transient": escalate it, with the code path if you find it.
- **environment:** the machine, credentials, a dependency, the network, a third-party tool.
- **spec:** the campaign's brief, evaluate command or config asked for something wrong or impossible.
- **model:** the agent gave up early, ignored the brief, or misjudged its own result.

Write one comment per root cause, on the row it is about: what happened, the class, the evidence, how sure you are,
and the fix. An escalation when someone must act, else a note.
