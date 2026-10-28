"""The guide lists that roles share, which each role extends with what only it uses (``RoleConfig.GUIDES``)."""

from piloop.plugins.role.contract import RoleConfig

READER = (*RoleConfig.GUIDES, 'concepts', 'judging', 'comments')
"""A role that reads the campaign's results and writes about them: what the rows mean, how a candidate is judged, and
how to comment."""
