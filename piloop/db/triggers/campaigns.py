"""The campaign tree: the persistent triggers on ``campaigns``.

One condition and one message, in a trigger for an INSERT and one for an UPDATE of ``parent_id``. A CHECK cannot
read another row, so the rule is a trigger.
"""

from piloop.db.triggers.ddl import Trigger

EVALUATIONS_RESUMED = Trigger(
    'campaigns_resume_untagged_evaluations',
    'campaigns',
    'AFTER UPDATE',
    of=('paused',),
    when="OLD.paused = 'all' AND NEW.paused IS NOT 'all'",
    body="DELETE FROM evaluation_task_failures WHERE campaign_id = NEW.id AND task_key = 'campaign' AND held",
    requires='evaluation_task_failures',
)
"""Release the shared evaluator hold when its campaign resumes, including a resume through SQL."""


def acyclic() -> tuple[Trigger, Trigger]:
    """Persistent: refuse a ``campaigns.parent_id`` that is the campaign itself or one of its descendants.

    The condition walks up from the new parent. A parent is a row of the same table, so it is in the same piloop home,
    and a campaign has at most one. On an INSERT, only the campaign itself can be its descendant: an explicit ``id``
    equal to ``parent_id``.
    """
    ancestors = (
        'WITH RECURSIVE up(id) AS (SELECT NEW.parent_id UNION SELECT p.parent_id FROM campaigns p JOIN up ON p.id = up.id'
        ' WHERE p.parent_id IS NOT NULL) SELECT id FROM up'
    )
    when = f'NEW.parent_id IS NOT NULL AND NEW.id IN ({ancestors})'
    body = (
        "SELECT RAISE(ABORT, 'campaigns.parent_id: a campaign cannot be under itself or under one of its descendants')"
    )
    return (
        Trigger('campaigns_parent_insert', 'campaigns', 'AFTER INSERT', when=when, body=body),
        Trigger('campaigns_parent_update', 'campaigns', 'AFTER UPDATE', of=('parent_id',), when=when, body=body),
    )
