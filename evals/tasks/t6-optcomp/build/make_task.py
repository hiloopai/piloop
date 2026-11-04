"""Build the t6-optcomp task from a silmaril-lean checkout: its repo, targets and sealed reference.

Usage:
    python3 -I make_task.py <silmaril-lean checkout> <work dir> <packages dir> [--variant headline|lemmas|v2]
        [--courses <module prefix>,...] [--template <task dir>]
        [--private <dir of private exercise files>]

<packages dir> holds Mathlib and its dependencies, built, at the revisions of silmaril-lean's
manifest. Writes, under <work dir>:

* ref/:   the course's import closure with its proofs (the known-good solution), built and audited.
* stub/:  the starting repo's Lean tree: definitions kept, the targets' proofs replaced by `sorry`;
          with `headline`, the lemmas that only serve other proofs are deleted too.
* spec.json: what the scorer compares against (targets, protected constants and their hashes).

Every step is checked: the stub must build, replay through the kernel, and agree with the reference
on the hash of every constant it keeps.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = (
    Path(sys.argv[sys.argv.index('--template') + 1]).resolve() if '--template' in sys.argv else HERE.parent
) / 'repo'
"""The task's repo: its hand-written files are kept, the rest is generated."""
KEPT = ('brief.md', 'evaluate.py', '.gitignore', 'LICENSE', 'check/Audit.lean')
AUDIT = TEMPLATE / 'check' / 'Audit.lean'
PREFIX = 'Silmaril.Courses.Cambridge.OptimisingCompilers'
TOOLCHAIN = Path.home() / '.elan/toolchains/leanprover--lean4---v4.35.0-rc4/bin'
PRIVATE = f'{PREFIX}.Private'
"""Where the private split's modules go: copied into a run's repo at its start, never into a repository."""
STANDARD = {'propext', 'Classical.choice', 'Quot.sound'}

CSLIB = """
[[require]]
name = "cslib"
git = "https://github.com/leanprover/cslib"
rev = "3c9db6a6a7207770c5a00849f4603ef460202dbf"
"""
"""The Apache-2.0 library of computer-science foundations that the semantics courses import; a Lake dependency."""

LAKEFILE = """name = "optcomp"
defaultTargets = ["Silmaril"]

[leanOptions]
autoImplicit = false

[[require]]
name = "mathlib"
scope = "leanprover-community"
git = "https://github.com/leanprover-community/mathlib4"
rev = "1f414401f69059aa7eead47b53ee40bd38455eeb"

[[lean_lib]]
name = "Silmaril"
globs = ["Silmaril.+"]
"""

CONTINUATION = re.compile(r'(\||termination_by|decreasing_by|where\b|[)\]}⟩]|:=)')
THEOREM = re.compile(r'^(?:@\[[^\n]*\]\s*)?(?:(?:private|protected|public|nonrec|noncomputable)\s+)*(theorem|lemma)\s')
THEOREM_LINE = re.compile(THEOREM.pattern, re.M)
EXAMPLE = re.compile(r'^(?:@\[[^\n]*\]\s*)?(?:noncomputable\s+)?example[\s:(]', re.M)
MODIFIER = re.compile(r'\bin$')  # `omit … in`, `variable … in`, `open … in`: part of the next declaration
PREAMBLE = re.compile(r'^(/--|@\[[^\]]*\]\s*$)')  # a docstring or attribute line that belongs to the next declaration


def imports(path: Path) -> list[str]:
    """The modules a Lean file imports."""
    return re.findall(r'^\s*(?:public\s+)?(?:meta\s+)?import\s+(?:all\s+)?([\w.]+)', path.read_text(), re.M)


def module_file(root: Path, module: str) -> Path:
    """The source file of ``module`` under ``root``."""
    return root / (module.replace('.', '/') + '.lean')


def extract(src: Path, out: Path, prefixes: list[str]) -> dict[str, list[str]]:
    """Copy the import closure of the courses into `out`; return their modules and the library's."""
    seeds = sorted(
        str(p.relative_to(src))[:-5].replace('/', '.')
        for prefix in prefixes
        for p in src.glob(prefix.replace('.', '/') + '/**/*.lean')
    )
    tops = [prefix for prefix in prefixes if module_file(src, prefix).exists()]
    seeds += tops
    seen, stack = set(), list(seeds)
    while stack:
        m = stack.pop()
        if m not in seen:
            seen.add(m)
            stack += [i for i in imports(module_file(src, m)) if i.startswith('Silmaril')]
    for m in seen:
        dst = module_file(out, m)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(module_file(src, m), dst)
    return {'course': sorted(set(seeds) - set(tops)), 'library': sorted(seen - set(seeds) | set(tops))}


def project(root: Path, src: Path, packages: Path) -> None:
    """Write the Lake project files, with cslib when a module imports it, and link the prebuilt packages."""
    cslib = any(re.search(r'^\s*(?:public\s+)?import\s+Cslib', p.read_text(), re.M) for p in root.rglob('*.lean'))
    (root / 'lakefile.toml').write_text(LAKEFILE + (CSLIB if cslib else ''))
    shutil.copy(src / 'lean-toolchain', root / 'lean-toolchain')
    manifest = json.loads((src / 'lake-manifest.json').read_text())
    manifest['packages'] = [p for p in manifest['packages'] if cslib or p['name'] != 'cslib']
    manifest['name'] = 'optcomp'
    (root / 'lake-manifest.json').write_text(json.dumps(manifest, indent=1) + '\n')
    (root / '.lake').mkdir(exist_ok=True)
    link = root / '.lake' / 'packages'
    if not link.exists():
        link.symlink_to(packages)


def env(root: Path, packages: Path) -> dict[str, str]:
    """The environment that runs Lean on ``root`` with the packages' build directories."""
    lean_path = [str(p / '.lake/build/lib/lean') for p in sorted(packages.iterdir())] + [
        str(root / '.lake/build/lib/lean')
    ]
    return {**os.environ, 'PATH': f'{TOOLCHAIN}:{os.environ["PATH"]}', 'LEAN_PATH': ':'.join(lean_path)}


def build(root: Path, packages: Path) -> None:
    """Build ``root``; exit with its errors when it fails."""
    done = subprocess.run(
        ['taskset', '-c', '0-3', 'nice', 'lake', 'build'],
        cwd=root,
        env=env(root, packages),
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        errors = [line for line in done.stdout.splitlines() if line.startswith('error')]
        raise SystemExit(f'build of {root} failed:\n' + '\n'.join(errors[:20]))


def audit(root: Path, packages: Path, modules: list[str]) -> dict:
    """The audit of ``modules`` as built in ``root``; exit when it fails or the kernel rejects a constant."""
    done = subprocess.run(
        ['taskset', '-c', '0-3', 'nice', 'lean', '--run', str(AUDIT), *modules],
        cwd=root,
        env=env(root, packages),
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise SystemExit(f'audit of {root} failed:\n{done.stderr[-2000:]}')
    result = json.loads(done.stdout)
    if result['replay'] != 'ok':
        raise SystemExit(f'kernel replay of {root} failed: {result["replay"][:2000]}')
    return result


def groups(text: str) -> list[tuple[list[int], str]]:
    """The file as (piece first lines, text) groups: one per command.

    A docstring, an attribute line or a `… in` modifier is joined to the declaration that follows it. Lean's
    declaration range starts at one of the pieces (the docstring, not a `variable … in` before it).
    """
    lines = text.split('\n')
    starts, depth = [0], 0
    for i, line in enumerate(lines):
        if i and depth == 0 and line and not line[0].isspace() and not CONTINUATION.match(line):
            starts.append(i)
        depth = max(0, depth + line.count('/-') - line.count('-/'))
    pieces = [
        (s, '\n'.join(lines[s : starts[k + 1] if k + 1 < len(starts) else len(lines)])) for k, s in enumerate(starts)
    ]
    out: list[tuple[list[int], str]] = []
    pending: tuple[list[int], str] | None = None
    for s, block in pieces:
        preamble = bool(PREAMBLE.match(block) or MODIFIER.search(block.rstrip()))
        starts_, joined = [s], block
        if pending is not None:
            starts_, joined = pending[0] + [s], pending[1] + '\n' + block
            pending = None
        if preamble:
            pending = (starts_, joined)
            continue
        out.append((starts_, joined))
    if pending is not None:
        out.append(pending)
    return out


def proof_start(decl: str) -> int | None:
    """The offset where a theorem's proof starts.

    That is the first `:=` outside brackets (not a `let`'s), or the first line that starts with `|`.
    """
    head = THEOREM_LINE.search(decl)
    if head is None:
        return None
    depth, i = 0, head.start()
    while i < len(decl):
        c = decl[i]
        if decl.startswith('--', i):
            i = decl.find('\n', i)
            if i < 0:
                return None
            continue
        if decl.startswith('/-', i):
            i = decl.find('-/', i) + 2
            continue
        if c in '([{⟨⦃':
            depth += 1
        elif c in ')]}⟩⦄':
            depth -= 1
        elif depth == 0 and decl.startswith(':=', i):
            if not re.search(r'\b(let|have)\b', decl[decl.rfind('\n', 0, i) + 1 : i]):
                return i
        elif depth == 0 and c == '\n' and re.match(r'\n[ \t]*\|', decl[i:]):
            return i
        i += 1
    return None


def rewrite(root: Path, modules: list[str], action: dict[tuple[str, int], str]) -> int:
    """Apply `sorry` / `delete` to the theorems at (module, 1-based line) and delete every `example`.

    Returns how many declarations changed.
    """
    changed = 0
    for m in modules:
        path = module_file(root, m)
        pieces = []
        for starts, block in groups(path.read_text()):
            s = starts[0]
            what = next((action[(m, t + 1)] for t in starts if (m, t + 1) in action), None)
            if EXAMPLE.search(block):  # unnamed, so never scored; its proof would be a free hint
                changed += 1
                continue
            if what and THEOREM_LINE.search(block):  # not a theorem: one generated by `@[ext]` and the like
                if what == 'delete':
                    changed += 1
                    continue
                at = proof_start(block)
                if at is None:
                    raise SystemExit(f'no proof in {m}:{s + 1}')
                block = block[:at].rstrip() + ' := sorry' + block[len(block.rstrip()) :]
                changed += 1
            pieces.append(block)
        path.write_text('\n'.join(pieces))
    return changed


def internal(name: str) -> bool:
    """A name Lean generates: an auxiliary proof, a matcher, a private prefix aside."""
    name = re.sub(r'^_private\.[\w.]+?\.0\.', '', name)
    return any(p.startswith('_') or re.match(r'(match|proof)_\d+$', p) for p in name.split('.'))


def main() -> None:
    """Build the task; see the module's docstring."""
    src, work, packages = (Path(a).resolve() for a in sys.argv[1:4])
    variant = sys.argv[sys.argv.index('--variant') + 1] if '--variant' in sys.argv else 'headline'
    ref, stub = work / 'ref', work / 'stub'
    for d in (ref, stub):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    prefixes = sys.argv[sys.argv.index('--courses') + 1].split(',') if '--courses' in sys.argv else [PREFIX]
    mods = extract(src, ref, prefixes)
    everything = mods['course'] + mods['library']
    project(ref, src, packages)
    build(ref, packages)
    reference = audit(ref, packages, everything)
    (work / 'reference-audit.json').write_text(json.dumps(reference))
    R = {c['name']: c for c in reference['constants']}
    course = set(mods['course'])
    theorems = {
        n: c
        for n, c in R.items()
        if c['kind'] == 'theorem' and c['module'] in course and c.get('line') and not internal(n)
    }

    # direct uses between the course's theorems, seen through the auxiliary constants proofs generate
    def uses(n: str, seen: set[str]) -> set[str]:
        out = set()
        for u in R.get(n, {}).get('uses', []):
            if u in theorems:
                out.add(u)
            elif u in R and u not in seen and (internal(u) or not R[u].get('line')):
                seen.add(u)
                out |= uses(u, seen)
        return out

    used_by: dict[str, set[str]] = {n: set() for n in theorems}
    for n in theorems:
        for u in uses(n, set()) - {n}:
            used_by[u].add(n)
    given: set[str] = set()  # lemmas a definition needs keep their proofs, as do the lemmas those use
    frontier = [u for n, c in R.items() if c['kind'] != 'theorem' for u in uses(n, set())]
    while frontier:
        n = frontier.pop()
        if n not in given:
            given.add(n)
            frontier += list(uses(n, set()))
    roots = {n for n in theorems if not used_by[n]} - given
    # v2 proves the lemmas the definitions need too: they are stated, as targets, and nothing is given
    stated = roots | given if variant == 'v2' else roots
    duplicates = twins(R, stated, given)
    if variant in ('headline', 'v2'):
        action = {
            (c['module'], c['line']): 'sorry' if n in stated - duplicates else 'delete'
            for n, c in theorems.items()
            if variant == 'v2' or n not in given
        }
    else:
        action = {(c['module'], c['line']): 'sorry' for n, c in theorems.items() if n not in given}
    if variant == 'v2':
        given = set()

    shutil.copytree(ref, stub, dirs_exist_ok=True, ignore=shutil.ignore_patterns('.lake'))
    project(stub, src, packages)
    changed = rewrite(stub, mods['course'], action)
    build(stub, packages)
    S = audit(stub, packages, everything)
    consts = {c['name']: c for c in S['constants']}
    # a given lemma whose proof leans on a target is proved once the target is: not a target of its own
    targets, protected = check(
        reference, S, {n: c for n, c in theorems.items() if n not in given}, set(everything), given
    )
    out = {
        'variant': variant,
        'source': 'hiloopai/silmaril-lean',
        'source_revision': git_rev(src),
        'modules': everything,
        'targets': targets,
        'given': sorted(given & set(consts)),
        'standard_axioms': sorted(STANDARD),
        'protected': by_module(protected, consts),
    }
    (work / 'spec.json').write_text(json.dumps(out, indent=1) + '\n')
    assemble(work, out)
    if '--private' in sys.argv:
        private_split(work, Path(sys.argv[sys.argv.index('--private') + 1]).resolve(), packages, everything)
    print(
        f'{variant}: {changed} theorems rewritten, {len(targets)} targets, {len(given)} given, '
        f'{len(protected)} protected constants, stub agrees with the reference'
    )


def by_module(protected: dict[str, dict[str, str]], consts: dict) -> dict[str, dict[str, str]]:
    """The protected constants as ``{module: {name: "kind hash"}}``: the evaluator's format, each name once."""
    out: dict[str, dict[str, str]] = {}
    for name in sorted(protected):
        out.setdefault(consts[name]['module'], {})[name] = f'{protected[name]["kind"]} {protected[name]["hash"]}'
    return out


def twins(R: dict, stated: set[str], given: set[str]) -> set[str]:
    """The stated theorems whose statement another one has already: each is deleted, so no proof counts twice.

    Of a group with one statement, a lemma a definition needs is kept first, then the first by name; a definition's
    lemma is never deleted.
    """
    groups: dict[str, list[str]] = {}
    for n in stated:
        groups.setdefault(R[n]['hash'], []).append(n)
    out: set[str] = set()
    for names in groups.values():
        _, *rest = sorted(names, key=lambda n: (n not in given, n))
        out |= {n for n in rest if n not in given}
    return out


def check(
    reference: dict, stub: dict, theorems: dict, modules: set[str], given: frozenset[str] | set[str] = frozenset()
) -> tuple[list[str], dict]:
    """The targets and protected constants of ``modules``, after checking the stub against the reference."""
    R = {c['name']: c for c in reference['constants']}
    consts = {c['name']: c for c in stub['constants'] if c['module'] in modules}
    sorried = {a['name'] for a in stub['axioms'] if 'sorryAx' in a['axioms'] and a['name'] in consts}
    targets = sorted(n for n in sorried if n in theorems)
    # a declared theorem that depends on `sorry` must be a target; generated ones (`mk.injEq`) may
    stray = sorted(
        n
        for n in sorried - set(given)
        if n not in theorems and n in R and R[n].get('line') and R[n]['kind'] == 'theorem' and not internal(n)
    )
    # everything but a theorem keeps its meaning, and so does every theorem that is not a target;
    # macro expansions (`_aux_…`) hold source positions and are left out
    protected = {
        n: {'kind': c['kind'], 'hash': c['hash']}
        for n, c in consts.items()
        if n in R
        and not n.split('.')[-1].startswith('_aux_')
        and not n.startswith('_aux_')
        and (c['kind'] != 'theorem' or not internal(n))
    }
    differ = sorted(n for n, c in protected.items() if (R[n]['kind'], R[n]['hash']) != (c['kind'], c['hash']))
    if differ or stray:
        raise SystemExit(f'stub disagrees with the reference: {differ[:10]} stray sorries: {stray[:10]}')
    bad = [a for a in reference['axioms'] if a['name'] in set(targets) and not set(a['axioms']) <= STANDARD]
    if bad:
        raise SystemExit(f'reference uses non-standard axioms: {bad[:5]}')
    return targets, protected


def private_split(work: Path, private: Path, packages: Path, public: list[str]) -> None:
    """Add the private exercise files: every theorem in them is a target.

    Writes ``private/`` (the stub files and ``spec.private.json``, copied into a run's repo from sealed storage),
    the private reference proofs to ``solutions/private-known-good``, and ``spec.private.json`` to ``sealed/``.
    """
    ref, stub = work / 'ref', work / 'stub'
    names = sorted(p.stem for p in private.glob('*.lean'))
    mods = [f'{PRIVATE}.{n}' for n in names]
    for root in (ref, stub):
        module_file(root, mods[0]).parent.mkdir(parents=True, exist_ok=True)
        for n, m in zip(names, mods, strict=True):
            shutil.copy(private / f'{n}.lean', module_file(root, m))
    everything = public + mods
    build(ref, packages)
    reference = audit(ref, packages, everything)
    R = {c['name']: c for c in reference['constants']}
    theorems = {
        n: c
        for n, c in R.items()
        if c['kind'] == 'theorem' and c['module'] in set(mods) and c.get('line') and not internal(n)
    }
    # a private theorem that a public one already states would carry a public proof: delete it, or, when another
    # private proof uses it, keep it proved and out of the targets
    hashes = {c['hash'] for c in R.values() if c['kind'] == 'theorem' and c['module'] in set(public)}
    used = {u for c in theorems.values() for u in c.get('uses', [])}
    restated = {n for n, c in theorems.items() if c['hash'] in hashes}
    action = {
        (c['module'], c['line']): 'delete' if n in restated else 'sorry'
        for n, c in theorems.items()
        if n not in restated & used
    }
    changed = rewrite(stub, mods, action)
    theorems = {n: c for n, c in theorems.items() if n not in restated}
    build(stub, packages)
    S = audit(stub, packages, everything)
    targets, protected = check(reference, S, theorems, set(mods), restated & used)
    consts = {c['name']: c for c in S['constants']}
    spec = {
        'split': 'private',
        'modules': mods,
        'targets': targets,
        'standard_axioms': sorted(STANDARD),
        'protected': by_module(protected, consts),
    }
    out, good = work / 'private', work / 'solutions' / 'private-known-good'
    for d in (out, good):
        if d.exists():
            shutil.rmtree(d)
    for m in mods:
        for root, dst in ((stub, out), (ref, good)):
            rel = module_file(Path(), m)
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(root / rel, dst / rel)
    text = json.dumps(spec, indent=1) + '\n'
    (out / 'spec.private.json').write_text(text)
    (work / 'sealed' / 'spec.private.json').write_text(text)
    print(f'private: {changed} theorems rewritten, {len(targets)} targets, {len(protected)} protected constants')


SCRUB = [
    (re.compile(r'Course developments following [^,]+, with'), 'Course developments, with'),
    (
        re.compile(
            r"A chapter-by-chapter index of [^\n]*'s lecture notes,\nversion of [^\n]*\[\w+\], building on notes by\n[^.]+\."
        ),
        "A chapter-by-chapter index of the course's lecture notes.",
    ),
    (re.compile(r'`[a-z]+\d{4}lecture` in `references.bib`'), 'the lecture notes'),
]
"""Comments that name a course's lecturers: the public evals repo names no one. Citations of papers stay."""


def scrub(root: Path) -> None:
    """Apply ``SCRUB`` to the Lean files under ``root``. Comments only: no constant's hash changes."""
    for path in root.rglob('*.lean'):
        text = path.read_text()
        new = text
        for pattern, replacement in SCRUB:
            new = pattern.sub(replacement, new)
        if new != text:
            path.write_text(new)


def allowed_commands(root: Path) -> dict[str, dict[str, int]]:
    """The banned commands (the evaluator's ``BANNED``) that the starting repo's own modules use, and how often."""
    spec = importlib.util.spec_from_file_location('evaluate', TEMPLATE / 'evaluate.py')
    assert spec is not None and spec.loader is not None
    evaluate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluate)
    found = {name: evaluate.banned(path.read_text()) for name, path in evaluate.sources(root).items()}
    return {name: dict(counts) for name, counts in found.items() if counts}


def assemble(work: Path, spec: dict) -> None:
    """Write the task's starting repo, its sealed data and the known-good solution."""
    repo, sealed, good = work / 'repo', work / 'sealed', work / 'solutions' / 'known-good'
    for d in (repo, sealed, good):
        if d.exists():
            shutil.rmtree(d)
    shutil.copytree(work / 'stub', repo, ignore=shutil.ignore_patterns('.lake'))
    for name in KEPT:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(TEMPLATE / name, repo / name)
    scrub(repo)
    spec['allowed'] = allowed_commands(repo)
    (work / 'spec.json').write_text(json.dumps(spec, indent=1) + '\n')
    shutil.copy(work / 'spec.json', repo / 'spec.json')
    sealed.mkdir(parents=True)
    for name in ('lakefile.toml', 'lake-manifest.json', 'lean-toolchain', 'spec.json'):
        shutil.copy(repo / name, sealed / name)
    lean = HERE / 'lean'  # the project files the image's build fetches Mathlib's cache with
    lean.mkdir(exist_ok=True)
    for name in ('lakefile.toml', 'lake-manifest.json', 'lean-toolchain'):
        shutil.copy(repo / name, lean / name)
    shutil.copy(AUDIT, sealed / 'Audit.lean')
    (sealed / 'meta.json').write_text(json.dumps({'split': 'all'}) + '\n')
    shutil.copytree(work / 'ref' / 'Silmaril', good / 'Silmaril')


def git_rev(src: Path) -> str:
    """The commit of the checkout ``src``."""
    return subprocess.run(['git', '-C', str(src), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()


if __name__ == '__main__':
    main()
