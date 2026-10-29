"""``structured``: an escalation asks for one decision in plain words, offers choices, and goes to the orchestrator first.

The form has an ``ask`` (one sentence), a ``why`` (a few short sentences), two or more ``options``, each a ``choice`` and
its ``result``, an optional ``recommend``, and ``details`` for the evidence. The ask, the why and the options follow
the rules of Simplified Technical English that a program can check: short sentences, and no ids, hashes or paths,
which go in ``details``. The limits are keys.

An escalation of the orchestrator or of the daemon reaches a human at once. Any other agent's goes to the orchestrator,
which answers it, merges it with its duplicates, or escalates it itself; it reaches a human only when nobody has
acknowledged it ``grace`` after it was filed.
"""

import json
import re
import string
from datetime import datetime, timedelta
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from piloop import deprecated
from piloop.actor import ACTORS, Agent, System
from piloop.plugins.escalation.contract import At, Choice, Escalated, EscalationPolicy, EscalationView, Now, ToHuman

# TODO(skills slot): give the orchestrator its triage guidance from here, as SkillPart(skill='piloop-operate',
# order=350) and the launched orchestrator's Guide, once `piloop.skills` is on main. Until then the guidance is core
# text: "Escalations" in role/prompts/orchestrator.md, and step 2 of "Escalations" in skills/piloop-operate/SKILL.md.
SENTENCE = re.compile(r'(?<=[.!?])\s+')
"""Where one sentence ends and the next starts."""
JARGON = {
    'an id': re.compile(r'\b[a-z_]+#\d+\b'),
    'a hash': re.compile(r'\b[0-9a-f]{7,}\b'),
    'a path': re.compile(r'(?:\b[\w.-]+/[\w./-]*\w|\b[\w-]+\.(?:py|json|jsonl|toml|md|sh|yaml|yml|csv|txt|log)\b)'),
}
"""What a reader cannot read at a glance, and what ``details`` holds instead."""
IDS = string.ascii_uppercase
"""The ids of the choices, in order: A, B, C…"""


class Option(BaseModel):
    """One answer to the ask."""

    model_config = ConfigDict(extra='forbid', frozen=True)

    choice: str = Field(min_length=1)
    """What the human chooses, such as "Raise the timeout to 4 hours."."""
    result: str = Field(min_length=1)
    """What happens then, such as "Each run costs 3 more GPU hours."."""


class Form(BaseModel):
    """What ``piloop escalate --form`` takes, as JSON."""

    model_config = ConfigDict(extra='forbid', frozen=True)

    ask: str = Field(min_length=1)
    """The decision that is needed, in one sentence."""
    why: str = Field(min_length=1)
    """Why it is needed, in a few short sentences."""
    options: list[Option] = Field(min_length=2)
    recommend: int | None = None
    """The index of the option that you recommend, from 0, or none."""
    details: str = ''
    """The evidence: ids, hashes, paths, numbers, logs."""

    @model_validator(mode='after')
    def _recommend_is_an_option(self) -> Self:
        if self.recommend is not None and not 0 <= self.recommend < len(self.options):
            raise ValueError(f'recommend: {self.recommend} is no option; the options are 0 to {len(self.options) - 1}')
        return self


EXAMPLE = Form(
    ask='Do you approve a new evaluator version for the word-plan stage?',
    why='The frozen evaluator rejects changes to the stage files. Phase B needs a new stage. Six runs stopped on this.',
    options=[
        Option(
            choice='Approve a new evaluator version.', result='Phase B can start. Later scores use the new version.'
        ),
        Option(choice='Keep the evaluator frozen.', result='Phase B stops. The runs work on other ideas.'),
    ],
    recommend=0,
    details='candidate#1 is frozen. ladder.enforce_component_diff refuses stages.py and pipeline.py.',
)


class Structured(EscalationPolicy):
    """``structured``: one decision in plain words, with choices; an agent's goes to the orchestrator first."""

    grace: timedelta = Field(default=timedelta(minutes=30), ge=timedelta(0))
    """How long an agent's escalation waits for the orchestrator before it goes to a human too. 0: at once."""
    ask_words: int = Field(default=25, gt=0)
    """The most words that the ask has."""
    why_sentences: int = Field(default=3, gt=0)
    """The most sentences that the why has."""
    sentence_words: int = Field(default=20, gt=0)
    """The most words of each sentence of the why."""
    choice_words: int = Field(default=20, gt=0)
    """The most words of each choice and of each result."""
    max_options: int = Field(default=5, ge=2)
    """The most options that an escalation offers."""

    def form(self, raw: JsonValue) -> JsonValue:
        try:
            form = Form.model_validate(raw)
        except ValidationError as e:
            fixes = [f'{".".join(map(str, err["loc"])) or "form"}: {err["msg"]}' for err in e.errors()]
            raise ValueError('; '.join(fixes)) from e
        if fixes := self._fixes(form):
            raise ValueError('; '.join(fixes))
        return form.model_dump(mode='json')

    def _fixes(self, form: Form) -> list[str]:
        """What breaks the rules of plain words, by field."""
        fixes: list[str] = []
        if len(SENTENCE.split(form.ask.strip())) > 1:
            fixes.append('ask: write one sentence')
        fixes += _long('ask', form.ask, self.ask_words)
        sentences = SENTENCE.split(form.why.strip())
        if len(sentences) > self.why_sentences:
            fixes.append(f'why: {len(sentences)} sentences, at most {self.why_sentences}')
        fixes += [
            fix for n, s in enumerate(sentences) for fix in _long(f'why sentence {n + 1}', s, self.sentence_words)
        ]
        if len(form.options) > self.max_options:
            fixes.append(f'options: {len(form.options)}, at most {self.max_options}')
        for n, option in enumerate(form.options):
            fixes += _long(f'options.{n}.choice', option.choice, self.choice_words)
            fixes += _long(f'options.{n}.result', option.result, self.choice_words)
        texts = {'ask': form.ask, 'why': form.why} | {
            f'options.{n}': f'{o.choice} {o.result}' for n, o in enumerate(form.options)
        }
        fixes += [
            f'{field}: {what} ({found.group()}) goes in details'
            for field, text in texts.items()
            for what, pattern in JARGON.items()
            if (found := pattern.search(text)) is not None
        ]
        return fixes

    def draft(self, asked: EscalationView) -> JsonValue:
        ids = [c.id for c in asked.choices]
        form = Form(
            ask=asked.headline,
            why=asked.why or asked.headline,
            options=[Option(choice=c.label, result=c.result) for c in asked.choices],
            recommend=ids.index(asked.recommended) if asked.recommended in ids else None,
            details=asked.details,
        )
        return form.model_dump(mode='json')

    def guide(self) -> str:
        return (
            'Write an escalation in plain words, as JSON on stdin:\n'
            f'- ask: one sentence of at most {self.ask_words} words: the decision that you need.\n'
            f'- why: at most {self.why_sentences} sentences of at most {self.sentence_words} words each.\n'
            f'- options: 2 to {self.max_options}, each a "choice" and its "result", at most {self.choice_words} words'
            ' each.\n'
            '- recommend: the index of the option that you recommend, from 0 (optional).\n'
            '- details: the evidence. Ids such as idea#12, hashes and file paths go here, and nowhere else.\n'
            'Use short, common words, the active voice and the present tense. The human reads the ask first.\n'
            f'Example:\n{json.dumps(EXAMPLE.model_dump(mode="json"), indent=2)}'
        )

    def body(self, form: JsonValue) -> str:
        view = self._view(Form.model_validate(form))
        lines = [view.headline, '', view.why, '']
        lines += [_choice_line(c, c.id == view.recommended) for c in view.choices]
        return '\n'.join([*lines, '', 'Details:', view.details] if view.details else lines)

    def reach(self, item: Escalated, now: datetime) -> ToHuman:
        try:
            actor = ACTORS.validate_python(item.author)
        except ValidationError:  # a signature that an older piloop wrote: the human decides, at once
            return Now()
        match actor:
            case Agent() as agent if agent.party != System.ORCHESTRATOR.party:
                return At(item.filed + self.grace)
            case _:  # the orchestrator, the daemon, a person or a service
                return Now()

    def view(self, item: Escalated) -> EscalationView:
        if item.form is None:  # filed as free text: its first line asks, and the rest says why
            deprecated.until('0.5.0', 'an escalation filed as free text: refuse an escalation without a form')
            first, _, rest = item.body.strip().partition('\n')
            return EscalationView(headline=first, why=rest.strip())
        return self._view(Form.model_validate(item.form))

    def _view(self, form: Form) -> EscalationView:
        choices = tuple(Choice(id=IDS[n], label=o.choice, result=o.result) for n, o in enumerate(form.options))
        recommended = IDS[form.recommend] if form.recommend is not None else None
        return EscalationView(
            headline=form.ask, why=form.why, choices=choices, recommended=recommended, details=form.details
        )

    def choose(self, item: Escalated, choice: str) -> str:
        view = self.view(item)
        found = next((c for c in view.choices if c.id == choice.upper()), None)
        if found is None:
            offered = ', '.join(c.id for c in view.choices) or 'none: answer in words with `piloop inbox reply`'
            raise ValueError(f'comment#{item.comment} offers no choice {choice!r}; it offers {offered}')
        return _choice_line(found, False).replace(f'{found.id}. ', f'Chose {found.id}: ', 1)


def _long(field: str, text: str, limit: int) -> list[str]:
    words = len(text.split())
    return [f'{field}: {words} words, at most {limit}'] if words > limit else []


def _choice_line(choice: Choice, recommended: bool) -> str:
    mark = ' (recommended)' if recommended else ''
    return f'{choice.id}. {choice.label}{mark} → {choice.result}'
