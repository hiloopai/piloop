"""Tests for the sections: the typed blocks that every reader renders, and the sections that depend on their reader."""

from collections.abc import Iterator
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from piloop.api.views import CampaignOverview, Figure, Figures, Lines, Rendered, Table, Text
from piloop.db.models import Campaign, Pause
from piloop.db.session import Database
from piloop.ids import CampaignStatus
from piloop.plugins import host
from piloop.plugins.campaign_type.kit.runs.ids import RunEnd, RunStatus
from piloop.plugins.campaign_type.kit.runs.section.idea_comments import IdeaComments
from piloop.plugins.fact_source.kit.readings import Readings
from piloop.plugins.host import Scope
from piloop.plugins.role.contract import ROLES, ImplementerRole
from piloop.plugins.section.contract import SECTIONS, Guide, Rendering, Section, Surface, defaults, guides, markdown
from piloop.plugins.stop_rule.contract import STOP_RULES
from tests.campaign.test_stop import add_run
from tests.conftest import PLUGIN, cas_of, forget_loaded, install_plugins
from tests.daemon.test_scheduler import set_config
from tests.fixtures.plugin import Auditor
from tests.reports.test_overview import NOW


def test_a_section_reads_back_from_its_json_and_renders_each_block_kind_its_own_way() -> None:
    section = Rendered(
        title='Kinds',
        blocks=(
            Text(text='A paragraph.'),
            Lines(lines=('- one', '  - under it')),
            Figures(figures=(Figure(label='score', value=0.91234), Figure(label='spend', value=12, unit='USD'))),
            Table(columns=('candidate', 'score', 'summary'), rows=(('#1', None, 'a | b'), ('#2', 3.5, ''))),
        ),
    )
    shown = CampaignOverview(id=1, name='demo', path='/demo', parent=None, sections=[section])
    assert CampaignOverview.model_validate_json(shown.model_dump_json()) == shown, 'the client reads the same model'
    assert section.markdown() == snapshot("""\
## Kinds

A paragraph.

- one
  - under it

score: 0.9123 · spend: 12 USD

| candidate | score | summary |
|---|---|---|
| #1 | — | a / b |
| #2 | 3.5 |  |\
""")


def sections(*names: str) -> list[Section]:
    return [SECTIONS.validated({'provider': name}) for name in names]


def test_the_pack_is_the_research_pack(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        text = markdown(sections('pack'), Rendering(s, NOW, p, Path('/no/workspace'), cas_of(s)))
    assert text.startswith('## Research pack\n\n### Research pack: metrics')


def test_the_headline_says_why_the_campaign_is_held(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        p.status, p.stopped_reason = CampaignStatus.STOPPED, 'plateau'
        p.paused, p.paused_reason, p.paused_by = Pause.ALL, 'new data', 'human'
        text = markdown(sections('headline'), Rendering(s, NOW, p, Path('/no/workspace'), cas_of(s)))
    assert text.endswith(
        'stopped because: plateau · paused: launches and evaluations · paused by: human · paused because: new data'
    )


class KeepRate(Section):
    """A plugin's section: it declares where it shows by default, and where."""

    SHOWN_IN = frozenset({Surface.STATUS})
    ORDER = 172

    def render(self, r: Rendering) -> Rendered:
        return Rendered(title='Keep rate', blocks=(Figures(figures=(Figure(label='keeps per hour', value=0.5),)),))


@pytest.fixture
def keep_rate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    install_plugins(monkeypatch, [EntryPoint('keep_rate', f'{__name__}:KeepRate', SECTIONS.group)])
    yield
    forget_loaded()


def names(sections: list[Section]) -> list[str]:
    return [SECTIONS.name_of(s) for s in sections]


@pytest.mark.usefixtures('keep_rate')
def test_an_installed_section_shows_where_it_declares_in_its_order_unless_a_list_is_set() -> None:
    status = SECTIONS.used({})
    assert status[status.index('evaluations') + 1] == 'keep_rate', 'ORDER 172: between the evaluations and the funnel'
    assert 'keep_rate' not in defaults(Surface.ORCHESTRATOR), 'only where it declares'
    listed = SECTIONS.validated_table({'use': ['goal']}, Scope.CAMPAIGN)
    assert SECTIONS.used({'section': listed}) == ('goal',), 'a list in the configuration replaces the defaults'


def test_each_surface_shows_its_built_in_sections_by_default() -> None:
    overview = ['headline', 'goal', 'tracks', 'trajectory', 'runs', 'queue', 'agents', 'evaluations']
    overview += ['funnel', 'candidates', 'same_as_parent', 'inbox', 'events']
    assert defaults(Surface.STATUS) == overview == list(SECTIONS.used({}))
    assert defaults(Surface.ORCHESTRATOR) == ['context', *overview, 'tools', 'pack']
    assert defaults(Surface.RUN) == ['context', 'measure', 'idea_comments', 'history'], (
        'no board: a worker claims or proposes its idea with `piloop idea`'
    )
    assert ImplementerRole().shown() == ['context', 'measure', 'idea_comments', 'history'], 'no guides'


def test_an_implementer_is_told_how_its_change_is_measured(db: Database, campaign: Campaign) -> None:
    """The dev command, which piloop never runs, and that only what it submits is scored, up to its cap."""
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert markdown(sections('measure'), Rendering(s, NOW, p, Path('/no/workspace'), cas_of(s))) == snapshot("""\
## Measure a change

- `piloop submit` scores the version that your checkout is at, on the full evaluation. It returns at once: work on. Its score comes as a `[piloop]` message, with its gain over its parent, your line's best and the best. A score that beats the best is evaluated again until it is confirmed. Nothing is discarded: every score stays in the pool.
- Submit each version that beats your best so far. Only submitted versions are scored.\
""")
    dev = ['uv', 'run', 'evaluate.py', '--n', '50 rows']
    set_config(db, campaign, evaluator={'dev': dev, 'submissions': 2})
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        text = markdown(sections('measure'), Rendering(s, NOW, p, Path('/no/workspace'), cas_of(s)))
    assert text == snapshot("""\
## Measure a change

- `uv run evaluate.py --n '50 rows'` checks a change fast, in your checkout: piloop never runs it or records it.
- `piloop submit` scores the version that your checkout is at, on the full evaluation. It returns at once: work on. Its score comes as a `[piloop]` message, with its gain over its parent, your line's best and the best. A score that beats the best is evaluated again until it is confirmed. Nothing is discarded: every score stays in the pool.
- Submit each version that beats your best so far: your run has 2 submissions. Only submitted versions are scored.\
""")


def test_the_funnel_shows_how_far_the_attempts_got_and_where_the_others_ended(db: Database, campaign: Campaign) -> None:
    add_run(db, campaign)
    add_run(db, campaign, measured=False)
    add_run(db, campaign, status=RunStatus.FAILED, end=RunEnd.FAILED, measured=False)
    add_run(db, campaign, keep_score=4)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        text = markdown(sections('funnel'), Rendering(s, NOW, p, Path('/no/workspace'), cas_of(s)))
    assert text == snapshot("""\
## Funnel

ideas: 4 · attempts: 4 · reached the evaluator: 2 · measured: 2 · new bests: 1

| ended | unmeasured attempts |
|---|---|
| failed | 1 |
| finished | 1 |\
""")


def test_the_funnel_shows_nothing_without_the_source_of_its_fact(
    db: Database, campaign: Campaign, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_plugins(monkeypatch, [EntryPoint('goes_on', f'{PLUGIN}:GoesOn', STOP_RULES.group)])
    try:
        set_config(db, campaign, plugins={'stop_rule': {'provider': 'goes_on'}, 'fact_source': {'use': ['campaign']}})
        with db.session() as s:
            p = s.get_one(Campaign, campaign.id)
            assert markdown(sections('funnel'), Rendering(s, NOW, p, Path('/no/workspace'), cas_of(s))) == ''
    finally:
        host.close()


def test_cost_reads_the_metric_it_names_and_shows_nothing_without_a_spend_or_a_metric(
    db: Database, campaign: Campaign
) -> None:
    """piloop knows no cost: the section names the fact of the spend and the cost metric in its own entry."""
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert markdown(sections('cost'), Rendering(s, NOW, p, Path('/no/workspace'), cas_of(s))) == '', (
            'nothing to show'
        )
    spend = {'provider': 'command', 'command': ['true'], 'metrics': ['spend']}
    set_config(db, campaign, plugins={'fact_source': {'use': ['campaign', 'optimization', 'spend'], 'spend': spend}})
    add_run(db, campaign, keep_score=4)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        [_, _, reads] = p.config.fact_sources()
        assert isinstance(reads, Readings)
        reads.record(s, p, {'spend': 412.3})
        s.flush()
        cost = [SECTIONS.validated({'provider': 'cost', 'metric': 'size'})]
        text = markdown(cost, Rendering(s, NOW, p, Path('/no/workspace'), cas_of(s)))
        plain = markdown(sections('cost'), Rendering(s, NOW, p, Path('/no/workspace'), cas_of(s)))
    assert text == snapshot("""\
## Cost

spend: 412.3 · per new best: 412.3 · since the latest new best: 0

evaluations (size): 5 · per evaluation: 2.5 · without the evaluator: 407.3 · per attempt: 407.3\
""")
    assert plain == snapshot("""\
## Cost

spend: 412.3 · per new best: 412.3 · since the latest new best: 0\
""")


class Units(Guide):
    """A package's guide for every sweep."""

    TITLE = 'Units'
    TEXT = Path(__file__)
    SHOWN_IN = frozenset({Surface.ORCHESTRATOR})


@pytest.fixture
def units(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    install_plugins(monkeypatch, [EntryPoint('units', f'{__name__}:Units', SECTIONS.group)])
    yield
    forget_loaded()


@pytest.mark.usefixtures('auditor')
def test_each_role_has_guides_of_its_own_that_extend_a_shared_list() -> None:
    shown = {name: [n for n in p.model().shown() if n in guides()] for name, p in ROLES.providers().items()}
    assert shown == snapshot({'implementer': [], 'auditor': ['database']})
    assert not any(SECTIONS.providers()[n].model.SHOWN_IN for n in guides()), (
        'a built-in guide goes where a role names it'
    )


@pytest.mark.usefixtures('units')
def test_a_packages_guide_shows_in_every_role_of_its_surface_after_the_roles_own() -> None:
    assert 'units' not in Auditor(sections=['context']).shown(), 'a role that lists its sections shows those'
    shown = Auditor().shown()
    assert shown.index('database') < shown.index('units') < shown.index('context'), (
        "a guide goes after the role's own and before the other sections of a default list"
    )
    assert 'units' not in ImplementerRole().shown()


def test_a_guide_renders_with_its_campaigns_id_and_primary_metric(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        text = markdown(sections('scoring'), Rendering(s, NOW, p, Path('/no/workspace'), cas_of(s)))
    assert text.startswith('## Read the pool') and f'c.campaign_id = {campaign.id} ' in text and "'$.score'" in text


def test_the_comments_on_an_idea_show_only_in_the_brief_of_its_run(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert IdeaComments().render(Rendering(s, NOW, p, Path('/no/workspace'), cas_of(s))) is None, 'no reader'
