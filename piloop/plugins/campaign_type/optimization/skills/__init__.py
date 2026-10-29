"""The optimization campaign type's parts of the orchestrator skills (``piloop.cli.skills``)."""

from importlib.resources import files

from piloop.cli.skills import SkillPart

_HERE = files(__package__)
SETUP = SkillPart('piloop-setup', 100, _HERE / 'setup.md')
REVIEW = SkillPart('piloop-review', 100, _HERE / 'review.md')
OPERATE = SkillPart('piloop-operate', 200, _HERE / 'operate.md')
"""What ``piloop status`` shows of the research, ideas and seeds, steering and runs, and branches."""
VERDICTS = SkillPart('piloop-operate', 500, _HERE / 'verdicts.md')
"""Candidates, past research, and how to judge a candidate."""
