# enwik8: compress 10 MB of Wikipedia

Make `compressor.py` compress `data/dev.bin`, the first 10 MB of enwik8 (an English Wikipedia XML dump), into fewer
bytes.

## Rules

- `compressor.py` defines `compress(data: bytes) -> bytes` and `decompress(blob: bytes) -> bytes`.
  `decompress(compress(data))` must return exactly `data`.
- `decompress` gets only the compressed bytes. It must not read the input from anywhere else (`data/`, the network,
  a cache).
- Use only the Python standard library and numpy. Do not edit `evaluate.py` or `data/`.
- The final version is also scored on other Wikipedia text it has never seen, and every byte of every file you add or
  change counts against it. A dictionary tuned to `data/dev.bin` costs its size there and gains less.

## Measure

`uv run python evaluate.py` prints two metrics:

- `bytes`: the compressed size plus the size of `compressor.py`. Lower is better. This is the metric to improve.
- `seconds`: the CPU time to compress and decompress. It must stay below 120 s.

## Starting points

The baseline is zlib at level 9: 3.69 MB in about 0.5 s. The standard library also has bz2 (about 2.92 MB) and lzma
(about 2.72 MB at preset 9 extreme). Context mixing does much better on this text but is slow in pure Python. Text
transforms (frequent words, XML-aware preprocessing) help any back end.
