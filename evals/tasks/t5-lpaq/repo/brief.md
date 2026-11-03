# enwik8 with a context-mixing compressor

`lpaq1.cpp` is lpaq1, a fast context-mixing compressor. Make it compress `data/dev.bin`, the first 10 MB of enwik8
(an English Wikipedia XML dump), into fewer bytes.

## Rules

- The codec builds from the repo's C++ files with `g++ -O2`, and keeps lpaq1's command line: `codec 9 <in> <out>`
  compresses, `codec d <in> <out>` decompresses. The round trip must return exactly the input.
- The codec and everything you run get 8 GiB of memory: your machine has 8 GiB, and the evaluator caps the codec's
  address space at 8 GiB.
- The codec targets x86-64-v3: AVX2 at most. The evaluator refuses a codec with AVX-512 instructions, since a scoring
  machine may have none.
- Decompression gets only the compressed file. It must not read the input from anywhere else.
- Every byte of every file you add or change counts, xz-compressed: a model or dictionary tuned to `data/dev.bin`
  costs its size. The final version is also scored on Wikipedia text it has never seen.
- Compression plus decompression must stay under 120 s of CPU on the reference machine, with 5% for noise: the
  evaluator scales `raw_seconds` by lpaq1's own time on the machine (`ref_seconds`). Do not edit `evaluate.py` or `data/`.

## Measure

`uv run python evaluate.py` builds the codec and prints `bytes` (compressed size plus the xz size of the sources;
lower is better) and `seconds`.

## Where it stands

lpaq1 is about 25% larger than the best context-mixing compressors (paq8 and cmix) on enwik8, which are far slower.
Gains here come from better models, mixing and contexts within the CPU budget.
