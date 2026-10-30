"""``structured``, the default escalation provider: its form, its rules of plain words, its routing and its view."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import JsonValue

from piloop.actor import System
from piloop.api.views import escalation_lines
from piloop.campaign import escalations
from piloop.db.models import Campaign
from piloop.db.session import Database
from piloop.ids import CampaignId, CommentId
from piloop.plugins.escalation.contract import At, Choice, Escalated, EscalationView, Now
from piloop.plugins.escalation.structured import EXAMPLE, Structured

FILED = datetime(2026, 10, 28, 12, tzinfo=UTC)
GOOD: dict[str, JsonValue] = EXAMPLE.model_dump(mode='json')
OPTIONS: list[JsonValue] = [o.model_dump(mode='json') for o in EXAMPLE.options]


def item(author: str = 'implementer#12', form: object = GOOD, body: str = 'x') -> Escalated:
    stored = Structured().form(form) if isinstance(form, dict) else None
    return Escalated(CommentId(7), CampaignId(1), author, FILED, body, stored)


def test_a_good_form_is_stored_as_it_was_filed_and_its_body_is_plain_text() -> None:
    form = Structured().form(GOOD)
    assert form == GOOD
    assert Structured().body(form).splitlines() == [
        'Do you approve a new evaluator version for the word-plan stage?',
        '',
        'The frozen evaluator rejects changes to the stage files. Phase B needs a new stage. Six runs stopped on this.',
        '',
        'A. Approve a new evaluator version. (recommended) → Phase B can start. Later scores use the new version.',
        'B. Keep the evaluator frozen. → Phase B stops. The runs work on other ideas.',
        '',
        'Details:',
        'candidate#1 is frozen. ladder.enforce_component_diff refuses stages.py and pipeline.py.',
    ]
    assert Structured().body({**GOOD, 'details': '', 'recommend': None}).splitlines()[-1].startswith('B. Keep')


@pytest.mark.parametrize(
    ('changes', 'fix'),
    [
        ({'options': OPTIONS[:1]}, 'options: List should have at least 2 items'),
        ({'ask': ''}, 'ask: String should have at least 1 character'),
        ({'extra': 1}, 'extra: Extra inputs are not permitted'),
        ({'recommend': 2}, 'recommend: 2 is no option; the options are 0 to 1'),
        ({'ask': 'Approve it? Now.'}, 'ask: write one sentence'),
        ({'ask': ' '.join(['word'] * 26)}, 'ask: 26 words, at most 25'),
        ({'why': 'One. Two. Three. Four.'}, 'why: 4 sentences, at most 3'),
        ({'why': ' '.join(['word'] * 21)}, 'why sentence 1: 21 words, at most 20'),
        ({'options': [OPTIONS[0]] * 6}, 'options: 6, at most 5'),
        ({'options': [{'choice': ' '.join(['w'] * 21), 'result': 'r'}] * 2}, 'options.0.choice: 21 words, at most 20'),
        ({'options': [{'choice': 'c', 'result': ' '.join(['w'] * 21)}] * 2}, 'options.0.result: 21 words, at most 20'),
        ({'ask': 'Fix idea#12?'}, 'ask: an id (idea#12) goes in details'),
        ({'why': 'It broke at 9f86d081.'}, 'why: a hash (9f86d081) goes in details'),
        ({'why': 'It reads src/stages.py.'}, 'why: a path (src/stages.py) goes in details'),
        (
            {'options': [{'choice': 'Edit stages.py.', 'result': 'r'}] * 2},
            'options.0: a path (stages.py) goes in details',
        ),
    ],
)
def test_a_form_that_breaks_a_rule_is_refused_with_what_to_fix(changes: dict[str, JsonValue], fix: str) -> None:
    with pytest.raises(ValueError, match=fix.replace('?', r'\?').replace('(', r'\(').replace(')', r'\)')):
        Structured().form({**GOOD, **changes})


def test_the_limits_are_keys() -> None:
    Structured(ask_words=30).form({**GOOD, 'ask': ' '.join(['word'] * 26)})
    with pytest.raises(ValueError, match='why: 2 sentences, at most 1'):
        Structured(why_sentences=1).form({**GOOD, 'why': 'One. Two.'})


def test_the_guide_names_the_fields_and_the_limits_and_shows_the_example() -> None:
    guide = Structured(ask_words=12).guide()
    assert 'ask: one sentence of at most 12 words' in guide and '"recommend": 0' in guide


@pytest.mark.parametrize(
    ('author', 'reach'),
    [
        ('implementer#12', At(FILED + timedelta(minutes=30))),
        ('research#3', At(FILED + timedelta(minutes=30))),
        ('orchestrator#9', Now()),
        ('orchestrator', Now()),
        ('orchestrator via human:alice', Now()),
        ('daemon', Now()),
        ('human:alice', Now()),
        ('not a signature #', Now()),
    ],
)
def test_an_agent_s_escalation_waits_for_the_orchestrator_and_the_rest_reach_a_human_at_once(
    author: str, reach: object
) -> None:
    assert Structured().reach(item(author), FILED) == reach


def test_the_view_has_the_ask_the_why_and_lettered_choices_and_a_free_text_one_reads_as_filed() -> None:
    assert Structured().view(item()) == EscalationView(
        headline=EXAMPLE.ask,
        why=EXAMPLE.why,
        choices=(
            Choice(
                id='A',
                label='Approve a new evaluator version.',
                result='Phase B can start. Later scores use the new version.',
            ),
            Choice(id='B', label='Keep the evaluator frozen.', result='Phase B stops. The runs work on other ideas.'),
        ),
        recommended='A',
        details=EXAMPLE.details,
    )


def test_a_choice_is_a_decision_in_words_and_an_unknown_one_is_refused() -> None:
    assert (
        Structured().choose(item(), 'b')
        == 'Chose B: Keep the evaluator frozen. → Phase B stops. The runs work on other ideas.'
    )
    with pytest.raises(ValueError, match="comment#7 offers no choice 'C'; it offers A, B"):
        Structured().choose(item(), 'C')


def test_inbox_show_prints_a_view_without_choices_as_its_ask_and_why() -> None:
    assert escalation_lines(
        EscalationView(headline='Is the SSO login ready?', why='Agents cannot log in.'), CommentId(3)
    ) == [
        'Is the SSO login ready?',
        '',
        'Agents cannot log in.',
    ]


def test_a_free_text_escalation_that_an_older_piloop_filed_takes_no_choice(db: Database, campaign: Campaign) -> None:
    """A row of 0.4 has no form: every surface shows its body, and a choice on it is refused."""
    with closing(sqlite3.connect(db.engine.url.database or '')) as con:  # as an older piloop wrote it: no TEMP trigger
        con.execute(
            'INSERT INTO comments (target_kind, target_id, author, kind, body, priority)'
            f" VALUES ('campaign', {campaign.id}, 'implementer#3', 'escalation', 'SSO expired', 1)"
        )
        con.commit()
    with db.session() as s, pytest.raises(escalations.Refused, match='filed as free text, with no choices'):
        escalations.choose(s, CommentId(1), 'A', author=System.HUMAN)
