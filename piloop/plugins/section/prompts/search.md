- `ideas_fts` (over title, hypothesis and plan) and `comments_fts` (over body) are full-text indexes of every row.
  Triggers keep them current. Read them. Never write them.
- "Has anyone tried soft voting" is one query. Run it before you propose, whatever the history table shows.
- Words are stemmed (vote = votes = voting) and accents are dropped.

Ideas by mechanism, best match first, with the matching words marked:

```sql
SELECT i.id, i.status, snippet(ideas_fts, -1, '[', ']', '…', 8) AS hit, bm25(ideas_fts) AS score
FROM ideas_fts JOIN ideas i ON i.id = ideas_fts.rowid
WHERE ideas_fts MATCH 'voting OR ensemble' AND i.campaign_id = <campaign id> ORDER BY score LIMIT 10;
```

Comments (findings, decisions, notes) that use a phrase, newest first:

```sql
SELECT c.target_kind, c.target_id, c.author, c.kind, snippet(comments_fts, 0, '[', ']', '…', 8) AS hit
FROM comments_fts JOIN comments c ON c.id = comments_fts.rowid
WHERE comments_fts MATCH '"target encoding"' ORDER BY c.id DESC LIMIT 10;
```

The MATCH string is FTS5 syntax, not free text:

- Words are combined with AND.
- `OR` is an operator. Write it in uppercase.
- `"double quotes"` make a phrase. Quotes are required around anything with punctuation (`"one-hot"`, `"k-fold"`).
  Without quotes, the hyphen reads as a column filter and the query fails with `no such column`.
- `ensem*` matches by prefix.
- To exclude a word, use the binary `voting NOT soft`, not a leading `-`.
- Abbreviations are separate words, so ask for both spellings: `HGBT OR "gradient boosting"`, `"k-NN" OR knn`.
- A lower `bm25()` is a better match. A term that almost every row has scores `-0.0` for all of them, because it
  carries no information. Read the snippets instead.
- Join the base table for anything except the text.
