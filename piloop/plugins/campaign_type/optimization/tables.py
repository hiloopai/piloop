"""The schema of ``optimization``: the tables of its components, in one chain until each component has its own."""

from pathlib import Path

from piloop.db.schema import Schema
from piloop.db.triggers import on
from piloop.plugins.campaign_type.kit.backlog.tables import Idea, IdeaLink, Technique
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluation, Evaluator, Measurement
from piloop.plugins.campaign_type.kit.runs.tables import Run

TABLES = (Evaluator, Idea, IdeaLink, Run, Candidate, Measurement, Evaluation, Technique)
SCHEMA = Schema(tables=TABLES, versions=Path(__file__).parent / 'migrations', triggers=on(TABLES))
