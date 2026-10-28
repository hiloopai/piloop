`piloop sql` runs one SQL statement on piloop's SQLite database: any statement except schema changes and transactions,
each a transaction of its own. Timestamps are UTC ISO-8601 strings.

Never put your own text inside a double-quoted shell argument: pipe it in. In double quotes, the shell runs what is in
backticks and `$(…)`, and expands `$VAR`, before piloop sees the text. `piloop sql -` reads the statement from stdin,
and a quoted heredoc (`<<'EOF'`, with the quotes) expands nothing:

```sh
piloop sql - <<'EOF'
INSERT INTO comments (target_kind, target_id, author, kind, body)
VALUES ('idea', 12, '<you>', 'note', 'Run `make bench` first: it''s the slow step.');
EOF
```

- In SQL, double each `'` of your text (`it''s`), as in any SQL string.
- Give other commands short values in single quotes, without a `'` in them. Write anything longer (Markdown, quoted
  commands) as a comment through `piloop sql -`, and point to it (`see comment#31`).
