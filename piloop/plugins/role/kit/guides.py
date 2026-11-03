"""The guide lists that roles share, which each role extends with what only it uses (``RoleConfig.GUIDES``)."""

from piloop.plugins.role.contract import RoleConfig

READER = (*RoleConfig.GUIDES, 'concepts', 'scoring', 'comments')
"""A role that reads the campaign's results and writes about them: what the rows mean, how a candidate is scored, and
how to comment."""
