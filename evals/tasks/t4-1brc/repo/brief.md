# One billion rows, lite: min, mean and max per weather station

`src/main.rs` reads a file of `<station>;<temperature>` lines and prints, for each station, `name=min/mean/max`, one
line each, sorted by the bytes of the name. Make it use less CPU time.

## Rules

- The output must be exactly the expected output: temperatures to one decimal, the mean rounded half up.
- A station name is 1 to 100 bytes of UTF-8 without `;` or a newline; a temperature is -99.9 to 99.9 with one
  decimal. A file can hold up to 10,000 distinct stations.
- Rust standard library only: no crates. Keep `Cargo.toml`'s profile, or change it if that helps.
- CPU time counts every thread, so more threads do not help: the program is single-threaded on purpose.
- Do not edit `evaluate.py` or `data/`.
- The final version is also scored on a file you have not seen, with other station names, as its CPU time against
  the starting program's on the same machine. A program tuned to these 413 names gains nothing there.

## Measure

- `uv run python evaluate.py --quick` builds (incrementally) and times the 10M-row file: five runs, median
  `cpu_seconds` (user plus system time of the program), and `wall_seconds`. A few seconds per run of the loop.
- `uv run python evaluate.py` does the same on the 50M-row file, three runs.

## Starting point

The program already maps the file, finds `;` eight bytes at a time while it
hashes the name, parses the temperature as integer tenths without branches, and counts into an open-addressing
table. Most easy ideas are taken: look for what still costs time per row.
