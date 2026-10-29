"""The ids and names of the ``evaluation`` component: its evaluators, measurements, rungs and tracks."""

from typing import Final, NewType

from piloop.ids import TargetKind

MEASUREMENT: Final = TargetKind('measurement')
"""A measurement of a candidate (``measurements``). What its comments and events point at."""

MeasurementId = NewType('MeasurementId', int)


EvaluatorId = NewType('EvaluatorId', int)


RungName = NewType('RungName', str)
"""The name of a rung of a campaign's evaluation ladder (``[[ladder]]``)."""


TrackName = NewType('TrackName', str)
"""A track's name: its primary's ``track``, or else the primary's name."""
