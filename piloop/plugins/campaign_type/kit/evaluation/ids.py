"""The ids and names of the ``evaluation`` component: its evaluators, measurements, rungs and tracks."""

from enum import StrEnum
from typing import Final, NewType

from piloop.ids import TargetKind

MEASUREMENT: Final = TargetKind('measurement')
"""A measurement of a candidate (``measurements``). What its comments and events point at."""

MeasurementId = NewType('MeasurementId', int)


EvaluatorId = NewType('EvaluatorId', int)


RungName = NewType('RungName', str)
"""The name of a rung of a campaign's evaluation ladder (``[[ladder]]``)."""


class Reading(StrEnum):
    """What one measurement says of its candidate (``Measurement.reading``)."""

    SCORE = 'score'
    """It measured the full panel, and succeeded: the only reading that scores."""
    CRASH = 'crash'
    """The evaluation failed, at a rung or on the full panel."""
    CLIMB = 'climb'
    """It passed a rung of the ladder: the climb goes on."""
    STOP = 'stop'
    """The climb stopped at a rung."""


class Measured(StrEnum):
    """Where the measurement of a candidate under one evaluator is (``Status.state``)."""

    UNMEASURED = 'unmeasured'
    """No measurement yet."""
    CLIMBING = 'climbing'
    """Its latest measurement passed a rung of the ladder."""
    STOPPED = 'stopped'
    """Its latest climb stopped at a rung: it has no score."""
    CRASHED = 'crashed'
    """Its latest evaluation failed, and none scored."""
    SCORED = 'scored'
    """At least one evaluation scored. The pool says whether enough did to confirm it (``Pool.confirmed``)."""


TrackName = NewType('TrackName', str)
"""A track's name: its primary's ``track``, or else the primary's name."""
