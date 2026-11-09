"""Research memory is removed: drop the ``lessons``, ``documents`` and ``journal`` tables, with their rows.

The rows are dropped, not archived: the backup that ``migrate`` writes first still has them. ``documents_fts`` goes with
``documents``, and each table's triggers and indexes with it. The ``memory/local`` chain goes too: its version table
and its row of ``migrations``. Each stored copy of a ``piloop.toml`` (``campaigns.config``) drops ``[plugins.memory]``
and the ``lessons`` section, as the ``research_memory`` bridge drops them from the file. The downgrade creates the
tables again, empty, and puts no key back: 0.6 reads each dropped key as its default.

Revision ID: 0085
Revises: 0084
"""

import json
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = '0085'
down_revision: str | None = '0084'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LESSONS = 'lessons'
"""The section of the lessons, which a stored config names in ``[plugins.section]`` and in a role's ``sections``."""

# Frozen here, as 0.6.0 left them: the tables, then the indexes, then the triggers.
SCHEMA = (
    'CREATE TABLE lessons (\n\tid INTEGER NOT NULL, \n\tcampaign_id INTEGER NOT NULL, \n\tfamily VARCHAR NOT NULL, \n'
    '\tstatus VARCHAR(16) NOT NULL, \n\teffect DOUBLE, \n\tmetric VARCHAR, \n\tbody VARCHAR NOT NULL, \n'
    '\tcreated_by VARCHAR NOT NULL, \n\tcreated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL, \n\tPRIMARY KEY (id), \n'
    '\tCONSTRAINT "lessons.effect and lessons.metric go together" CHECK ((effect IS NULL) = (metric IS NULL)), \n'
    "\tCONSTRAINT \"lessons.effect must be a number\" CHECK (typeof(effect) IN ('integer', 'real', 'null')), \n"
    '\tCONSTRAINT "lessons.family must be a text value" CHECK (typeof(family) IN (\'text\')), \n'
    '\tCONSTRAINT "lessons.body must be a text value" CHECK (typeof(body) IN (\'text\')), \n'
    '\tCONSTRAINT "lessons.created_by must be a text value" CHECK (typeof(created_by) IN (\'text\')), \n'
    "\tCONSTRAINT \"lessons.metric must be a text value\" CHECK (typeof(metric) IN ('text', 'null')), \n"
    '\tFOREIGN KEY(campaign_id) REFERENCES "campaigns" (id) ON DELETE CASCADE, \n'
    "\tCONSTRAINT lessonstatus CHECK (status IN ('dead', 'promising', 'untested'))\n)",
    'CREATE TABLE "documents" (\n\tid INTEGER NOT NULL, \n\tcampaign_id INTEGER NOT NULL, \n\tpath VARCHAR NOT NULL, \n'
    '\thash VARCHAR NOT NULL, \n\ttitle VARCHAR NOT NULL, \n\tbody VARCHAR NOT NULL, \n\tcreated_by VARCHAR NOT NULL, \n'
    '\tcreated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL, \n'
    '\tupdated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL, \n\tupdated_by VARCHAR, \n\tPRIMARY KEY (id), \n'
    '\tCONSTRAINT "documents.path must be a text value" CHECK (typeof(path) IN (\'text\')), \n'
    '\tCONSTRAINT "documents.created_by must be a text value" CHECK (typeof(created_by) IN (\'text\')), \n'
    '\tCONSTRAINT "documents.title must be a text value" CHECK (typeof(title) IN (\'text\')), \n'
    '\tCONSTRAINT "documents.body must be a text value" CHECK (typeof(body) IN (\'text\')), \n'
    '\tCONSTRAINT "documents.hash is the blake3 hex digest of body" CHECK '
    "(length(hash) = 64 AND hash NOT GLOB '*[^0-9a-f]*'), \n"
    '\tFOREIGN KEY(campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE, \n\tUNIQUE (campaign_id, path, hash)\n)',
    'CREATE TABLE "journal" (\n\tid INTEGER NOT NULL, \n\tauthor VARCHAR NOT NULL, \n\tline VARCHAR NOT NULL, \n'
    '\tcreated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL, \n\tcampaign_id INTEGER, \n\tPRIMARY KEY (id), \n'
    '\tCONSTRAINT "journal.author must be a text value" CHECK (typeof(author) IN (\'text\')), \n'
    '\tCONSTRAINT "journal.line is text of 1 to 280 bytes" CHECK '
    "(typeof(line) IN ('text') AND length(CAST(line AS BLOB)) BETWEEN 1 AND 280), \n"
    '\tCONSTRAINT journal_campaign FOREIGN KEY(campaign_id) REFERENCES campaigns (id)\n)',
    "CREATE VIRTUAL TABLE documents_fts USING fts5(title, body, content='documents', content_rowid='id', "
    "tokenize='porter unicode61 remove_diacritics 2')",
    'CREATE INDEX ix_lessons_campaign_family ON lessons (campaign_id, family)',
    'CREATE INDEX ix_journal_campaign ON journal (campaign_id)',
    'CREATE TRIGGER lessons_no_replace BEFORE INSERT ON lessons\nWHEN EXISTS (SELECT 1 FROM lessons WHERE id = NEW.id)\n'
    "BEGIN\n  SELECT RAISE(ABORT, 'lessons rows are never replaced: insert a new row');\nEND",
    'CREATE TRIGGER journal_no_replace BEFORE INSERT ON journal\nWHEN EXISTS (SELECT 1 FROM journal WHERE id = NEW.id)\n'
    "BEGIN\n  SELECT RAISE(ABORT, 'journal rows are never replaced: insert a new row');\nEND",
    'CREATE TRIGGER documents_updated_at AFTER UPDATE ON documents\n'
    'WHEN NEW.updated_at IS OLD.updated_at AND NEW.updated_by IS OLD.updated_by\n'
    "BEGIN\n  UPDATE documents SET updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE id = NEW.id;\nEND",
    'CREATE TRIGGER documents_fts_insert AFTER INSERT ON documents\n'
    'BEGIN\n  INSERT INTO documents_fts (rowid, title, body) VALUES (NEW.id, NEW.title, NEW.body);\nEND',
    'CREATE TRIGGER documents_fts_delete AFTER DELETE ON documents\n'
    "BEGIN\n  INSERT INTO documents_fts (documents_fts, rowid, title, body) VALUES ('delete', OLD.id, OLD.title, "
    'OLD.body);\nEND',
    'CREATE TRIGGER documents_fts_update AFTER UPDATE OF id, title, body ON documents\n'
    "BEGIN\n  INSERT INTO documents_fts (documents_fts, rowid, title, body) VALUES ('delete', OLD.id, OLD.title, "
    'OLD.body);\n  INSERT INTO documents_fts (rowid, title, body) VALUES (NEW.id, NEW.title, NEW.body);\nEND',
)


def upgrade() -> None:
    for table in ('documents_fts', 'documents', 'lessons', 'journal', 'alembic_version_memory__local'):
        op.execute(f'DROP TABLE IF EXISTS {table}')
    op.execute("DELETE FROM migrations WHERE name = 'memory/local'")
    bind = op.get_bind()
    for campaign_id, config in bind.execute(text('SELECT id, config FROM campaigns')).all():
        bind.execute(
            text('UPDATE campaigns SET config = :c WHERE id = :id'),
            {'c': json.dumps(dropped(json.loads(config))), 'id': campaign_id},
        )


def downgrade() -> None:
    connection = op.get_bind()
    for statement in SCHEMA:
        connection.exec_driver_sql(statement)


def dropped(config: dict[str, object]) -> dict[str, object]:
    """Return a stored config without ``[plugins.memory]`` and the ``lessons`` section. Frozen here, as of this revision."""
    plugins = config.get('plugins')
    if not isinstance(plugins, dict):
        return config
    plugins.pop('memory', None)
    sections = plugins.get('section')
    sections = sections if isinstance(sections, dict) else {}
    gone = {LESSONS} | {n for n, t in sections.items() if isinstance(t, dict) and t.get('provider') == LESSONS}
    for name in gone:
        sections.pop(name, None)
    if isinstance(sections.get('use'), list):
        sections['use'] = [n for n in sections['use'] if n not in gone]
    roles = plugins.get('role')
    for role in roles.values() if isinstance(roles, dict) else ():
        if isinstance(role, dict) and isinstance(role.get('sections'), list):
            role['sections'] = [n for n in role['sections'] if n not in gone]
    return config
