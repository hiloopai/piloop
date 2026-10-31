"""Merge approved pull requests, each as one squash commit, once a full CI run of its head is green.

    uv run -m scripts.merge --repo OWNER/NAME --remote REMOTE --messages DIR 590 591
    uv run -m scripts.merge --repo OWNER/NAME --remote REMOTE --messages DIR --train 577 578 579

Each pull request merges in the order given. One that fails, conflicts or is held is reported and skipped, and the rest
of the batch goes on.

``--train`` lands a stack, bottom first, from the green run of its top: only the top runs CI. The stack must sit on the
tip of its base, and the head of each lower pull request must be an ancestor of the top's head, so the top's run tested
every lower tree as part of its own. It labels each pull request ``train`` and marks drafts ready. Then, in order, it points each pull request at the stack's
base and waits until GitHub can merge it, marks its head with the top's green checks as commit statuses (which satisfy
a ruleset that requires those checks), and rebase-merges it. A rebase-merge gives commits new hashes, so the commits of
each pull request above are first replayed onto the merged ones, with the same trees, and pushed to its branch; CI
runs no tests on a labelled pull request. After each merge the base's tree must be that pull request's head's
tree, or the train stops: the base moved under it. Each commit lands as it is, so each subject
must be a Conventional Commit; the squash messages are not used.

``DIR`` holds the squash messages: ``titles.txt`` has a ``N|title`` line per pull request and ``N.md`` its body.
``names.txt``, if any, lists words that no squash message may contain, one per line. ``acked-comments.txt`` lists the
ids of review comments by ``--reviewer`` that were answered elsewhere. ``REMOTE`` must be the git remote of
``OWNER/NAME``: the script refuses to start otherwise, so a train never pushes to another repository.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

FULL_SUITE = 'coverage'
"""The CI job that runs only when the whole suite ran, and fails under 100% coverage: the merge requires it."""
GATE = 'check'
"""The CI job that is green when every other job of the run is."""
DO_NOT_MERGE = 'do-not-merge'
"""The label of a pull request that waits for a review, such as one that deletes tests."""
MERGEABLE = 60
"""Seconds to wait for GitHub to compute whether a pull request whose base just changed can merge."""
TRAIN = 'train'
"""The label of a pull request that a train lands: CI runs no tests on a head that the train replayed onto the one below."""
CONVENTIONAL = re.compile(r'(feat|fix|perf|revert|refactor|docs|test|ci|build|style|chore)(\([^)]+\))?!?: \S')
"""The subject of a commit that lands as it is: a type of CONTRIBUTING.md's table, as the PR title check allows."""
GITHUB_URL = re.compile(
    r'(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?'
)
DECLARATION = re.compile(r'^[^:]+:(?P<path>[^:]+):(?P<name>down_revision|revision)\b[^=]*=(?P<value>.*)$')


class Held(Exception):
    """The reason why a pull request does not merge now."""


@dataclass(frozen=True)
class PullRequest:
    """What a merge reads of a pull request."""

    number: int
    head: str
    head_ref: str
    base_ref: str
    draft: bool


@dataclass(frozen=True)
class Merger:
    """The merges into one repository, with the squash messages of one directory."""

    remote: str
    branch: str
    messages: Path
    reviewer: str | None

    def main(self) -> str:
        run('git', 'fetch', '-q', self.remote)
        return run('git', 'rev-parse', f'{self.remote}/{self.branch}')

    def pull(self, number: int) -> PullRequest:
        fields = 'state,headRefOid,headRefName,baseRefName,isDraft,labels'
        data = json.loads(gh('pr', 'view', str(number), '--json', fields))
        if data['state'] != 'OPEN':
            raise Held(f'is {data["state"].lower()}')
        if any(label['name'] == DO_NOT_MERGE for label in data['labels']):
            raise Held(f'is labelled {DO_NOT_MERGE}')
        return PullRequest(number, data['headRefOid'], data['headRefName'], data['baseRefName'], data['isDraft'])

    def checks(self, sha: str) -> dict[str, tuple[str, str]]:
        """The latest conclusion of each check of a commit and its page, by name; an unfinished one is ``pending``."""
        runs = json.loads(gh('api', '--paginate', '--slurp', f'repos/{{owner}}/{{repo}}/commits/{sha}/check-runs'))
        latest = sorted((r for page in runs for r in page['check_runs']), key=lambda r: r['id'])
        return {r['name']: (r['conclusion'] or 'pending', r['html_url']) for r in latest}

    def watch(self, pr: PullRequest) -> dict[str, str]:
        """Wait for the CI runs of the head; hold unless its gate and its full suite are green. Their pages, by name."""
        runs = json.loads(gh('run', 'list', '--commit', pr.head, '--json', 'databaseId,status'))
        for entry in runs:
            if entry['status'] != 'completed':
                subprocess.run(['gh', 'run', 'watch', str(entry['databaseId'])], capture_output=True)
        found = self.checks(pr.head)
        for job in (GATE, FULL_SUITE):
            if found.get(job, ('missing', ''))[0] != 'success':
                raise Held(f'{job} is {found.get(job, ("missing", ""))[0]} at {pr.head[:8]}')
        return {job: found[job][1] for job in (GATE, FULL_SUITE)}

    def message(self, number: int) -> tuple[str, str]:
        titles = (self.messages / 'titles.txt').read_text().splitlines()
        title = next((line.split('|', 1)[1] for line in titles if line.startswith(f'{number}|')), None)
        body = self.messages / f'{number}.md'
        if title is None or not body.is_file():
            raise Held(f'no title in titles.txt or no {body.name}')
        check_words(f'{title}\n{body.read_text()}', self.words())
        return f'{title} (#{number})', body.read_text()

    def words(self) -> list[str]:
        names = self.messages / 'names.txt'
        return [w for w in names.read_text().split() if w] if names.is_file() else []

    def unanswered(self, number: int) -> None:
        if self.reviewer is None:
            return
        acked_file = self.messages / 'acked-comments.txt'
        acked = set(acked_file.read_text().split()) if acked_file.is_file() else set()
        comments = json.loads(gh('api', '--paginate', '--slurp', f'repos/{{owner}}/{{repo}}/pulls/{number}/comments'))
        open_ids = [
            str(c['id'])
            for page in comments
            for c in page
            if c['user']['login'] == self.reviewer and c['in_reply_to_id'] is None and str(c['id']) not in acked
        ]
        if open_ids:
            raise Held(f'unanswered review comments {" ".join(open_ids)}')

    def merged_tree(self, base: str, head: str) -> str:
        result = subprocess.run(['git', 'merge-tree', '--write-tree', base, head], capture_output=True, text=True)
        if result.returncode:
            raise Held(f'conflicts with {base} at {head[:8]}')
        return result.stdout.split()[0]

    def merge(self, number: int) -> str:
        pr = self.pull(number)
        self.unanswered(number)
        subject, body = self.message(number)
        self.watch(pr)
        check_migrations(self.merged_tree(self.main(), pr.head))
        merged = subprocess.run(
            ['gh', 'pr', 'merge', str(number), '--squash', '--match-head-commit', pr.head]
            + ['--subject', subject, '--body', body],
            capture_output=True,
            text=True,
        )
        if merged.returncode:
            raise Held(f'merge refused: {merged.stderr.strip()}')
        return gh('pr', 'view', str(number), '--json', 'mergeCommit', '--jq', '.mergeCommit.oid')

    def batch(self, numbers: list[int]) -> list[int]:
        """Merge each in order; the ones held are returned."""
        held: list[int] = []
        for number in numbers:
            try:
                print(f'MERGED {number} {self.merge(number)}')
            except Held as reason:
                print(f'{number} HOLD: {reason}')
                held.append(number)
        return held

    def train(self, numbers: list[int]) -> None:
        """Land a stack, bottom first, by rebase-merging each pull request on GitHub, from the green run of its top."""
        prs = [self.pull(n) for n in numbers]
        for below, above in pairwise(prs):
            if above.base_ref != below.head_ref:
                raise Held(f'#{above.number} is based on {above.base_ref}, not on #{below.number}')
        top, base = prs[-1], f'{self.remote}/{prs[0].base_ref}'
        run('git', 'fetch', '-q', self.remote)
        check_stack(run('git', 'rev-parse', base), [(pr.number, pr.head) for pr in prs])
        for pr in prs:
            self.unanswered(pr.number)
        check_commits(run('git', 'log', '--format=%B%x00', f'{base}..{top.head}').split('\0')[:-1], self.words())
        pages = self.watch(top)
        check_migrations(self.merged_tree(base, top.head))
        gh(
            'label',
            'create',
            TRAIN,
            '--force',
            '--description',
            "landed by the merge script's train: CI tests only its top",
        )
        for pr in prs:
            gh('pr', 'edit', str(pr.number), '--add-label', TRAIN)
            if pr.draft:
                gh('pr', 'ready', str(pr.number))
        for below, pr in zip([None, *prs], prs, strict=False):
            head = pr.head
            if below is not None:
                # A rebase-merge gives the commits below new hashes: this pull request's own commits are replayed on
                # them, with the same trees, so that GitHub can rebase it in turn.
                head = replay(below.head, pr.head, run('git', 'rev-parse', base))
                lease = f'--force-with-lease=refs/heads/{pr.head_ref}:{pr.head}'
                run('git', 'push', '-q', lease, self.remote, f'{head}:refs/heads/{pr.head_ref}')
                gh('pr', 'edit', str(pr.number), '--base', prs[0].base_ref)
            self.wait_mergeable(pr.number)
            if head != top.head:
                self.vouch(head, top, pages)
            merged = subprocess.run(
                ['gh', 'pr', 'merge', str(pr.number), '--rebase', '--match-head-commit', head],
                capture_output=True,
                text=True,
            )
            if merged.returncode:
                raise Held(f'#{pr.number} merge refused: {merged.stderr.strip()}')
            run('git', 'fetch', '-q', self.remote)
            if run('git', 'rev-parse', f'{base}^{{tree}}') != run('git', 'rev-parse', f'{pr.head}^{{tree}}'):
                raise Held(f'#{pr.number} merged, but {base} is not its tree: the base moved under the train')
            print(f'MERGED {pr.number}')

    def wait_mergeable(self, number: int) -> None:
        """Wait until GitHub has computed that a pull request can merge; hold if it cannot."""
        deadline = time.monotonic() + MERGEABLE
        while (state := gh('pr', 'view', str(number), '--json', 'mergeable', '--jq', '.mergeable')) == 'UNKNOWN':
            if time.monotonic() > deadline:
                break
            time.sleep(5)
        if state != 'MERGEABLE':
            raise Held(f'#{number} cannot merge: GitHub says {state}')

    def vouch(self, head: str, top: PullRequest, pages: dict[str, str]) -> None:
        """Mark a head that the top's run tested with the top's green checks, as commit statuses that link to them."""
        for job, page in pages.items():
            gh(
                'api',
                '-X',
                'POST',
                f'repos/{{owner}}/{{repo}}/statuses/{head}',
                *('-f', 'state=success', '-f', f'context={job}', '-f', f'target_url={page}'),
                *('-f', f'description=green in the head of #{top.number}, {top.head[:8]}, which holds this one'),
            )


def run(*args: str) -> str:
    """The output of a command that must succeed."""
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def gh(*args: str) -> str:
    """The output of a ``gh`` command, in the repository that ``gh`` picks."""
    return run('gh', *args)


def replay(below: str, head: str, onto: str) -> str:
    """Copy the commits of ``below..head`` onto ``onto``, each with its own tree, author and message; the new head.

    ``onto`` must have the tree of ``below``: then each copy is the commit it copies, on new parents.
    """
    if run('git', 'rev-parse', f'{onto}^{{tree}}') != run('git', 'rev-parse', f'{below}^{{tree}}'):
        raise Held(f'{onto[:8]} is not the tree of {below[:8]}: the base moved under the train')
    parent = onto
    for commit in run('git', 'rev-list', '--reverse', f'{below}..{head}').split():
        name, email, date, message = run('git', 'log', '-1', '--format=%an%x00%ae%x00%aI%x00%B', commit).split('\0', 3)
        author = {'GIT_AUTHOR_NAME': name, 'GIT_AUTHOR_EMAIL': email, 'GIT_AUTHOR_DATE': date}
        parent = subprocess.run(
            ['git', 'commit-tree', f'{commit}^{{tree}}', '-p', parent, '-F', '-'],
            input=message,
            env={**os.environ, **author},
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    return parent


def check_stack(base: str, heads: list[tuple[int, str]]) -> None:
    """Hold unless a stack sits on the tip of its base and each lower head is an ancestor of the top's head.

    Only then did the top's green run test each lower tree as part of its own, and may a lower one carry its checks. On
    the tip, each rebase-merge leaves the base at that pull request's tree, and the next one applies on it.
    """
    bottom_number, bottom_head = heads[0]
    if subprocess.run(['git', 'merge-base', '--is-ancestor', base, bottom_head]).returncode:
        raise Held(f'#{bottom_number} at {bottom_head[:8]} is not on the tip of its base: rebase the stack on it')
    top_number, top_head = heads[-1]
    for number, head in heads[:-1]:
        if subprocess.run(['git', 'merge-base', '--is-ancestor', head, top_head]).returncode:
            raise Held(f'#{number} at {head[:8]} is not in the head of #{top_number}: rebase the stack on it')


def check_commits(messages: list[str], words: list[str]) -> None:
    """Hold unless each commit message, which lands as it is, has a Conventional Commit subject and no listed word."""
    for message in messages:
        subject = message.strip().splitlines()[0] if message.strip() else ''
        if not CONVENTIONAL.match(subject):
            raise Held(f'the commit subject {subject!r} is not a Conventional Commit')
        check_words(message, words)


def check_words(text: str, words: list[str]) -> None:
    """Hold if a text has a word of ``names.txt``."""
    if words and re.search(rf'\b({"|".join(map(re.escape, words))})\b', text, re.IGNORECASE):
        raise Held('a message that lands has a word of names.txt')


def check_remote(remote: str, repo: str) -> None:
    """Hold unless the remote's push URL is the GitHub repository ``OWNER/NAME`` that ``gh`` merges in.

    The trees that a merge checks come from the remote: a remote of another repository would check the wrong trees.
    """
    url = run('git', 'remote', 'get-url', '--push', remote)
    match = GITHUB_URL.fullmatch(url)
    if match is None or match['repo'].lower() != repo.lower():
        raise Held(f'the remote {remote} is {url}, not the repository {repo}')


def check_migrations(tree: str) -> None:
    """Each migrations directory of a tree declares each revision id once and has one head; holds if not."""
    found = subprocess.run(
        # no \b: git on macOS matches with the system's ERE, which lacks it; DECLARATION parses each line exactly
        ['git', 'grep', '-E', '^(down_)?revision', tree, '--', 'piloop/**/migrations/**.py'],
        capture_output=True,
        text=True,
    ).stdout
    declared: dict[str, dict[str, list[str]]] = {}
    for line in found.splitlines():
        if match := DECLARATION.match(line):
            declared.setdefault(match['path'], {})[match['name']] = re.findall(r"'([^']+)'", match['value'])
    directories: dict[str, list[tuple[str, list[str]]]] = {}
    for path, names in declared.items():
        for revision in names.get('revision', []):
            directories.setdefault(path.rsplit('/', 1)[0], []).append((revision, names.get('down_revision', [])))
    for directory, graph in directories.items():
        if twice := [revision for revision, count in Counter(r for r, _ in graph).items() if count > 1]:
            raise Held(f'{directory} declares revision {", ".join(twice)} twice: renumber')
        heads = {r for r, _ in graph} - {down for _, downs in graph for down in downs}
        if len(heads) != 1:
            raise Held(f'{directory} has the heads {", ".join(sorted(heads))}: renumber')


def parse() -> argparse.Namespace:
    """The command line."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        'numbers', type=int, nargs='+', help='the pull requests, in merge order (a train: bottom first)'
    )
    parser.add_argument('--repo', required=True, help='OWNER/NAME on GitHub')
    parser.add_argument('--remote', required=True, help="the git remote of the repository that gh picks, such as 'dev'")
    parser.add_argument('--messages', type=Path, required=True, help='the directory of the squash messages')
    parser.add_argument('--reviewer', help='a GitHub login whose unanswered review comments hold a merge')
    parser.add_argument('--train', action='store_true', help='land the pull requests as one stack')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse()
    # Every gh command below works in this repository. `gh repo view` reads the checkout instead: it is given the name.
    os.environ['GH_REPO'] = args.repo
    repository = json.loads(gh('repo', 'view', args.repo, '--json', 'nameWithOwner,defaultBranchRef'))
    try:
        check_remote(args.remote, repository['nameWithOwner'])
    except Held as reason:
        sys.exit(f'HOLD: {reason}')
    default_branch = repository['defaultBranchRef']['name']
    merger = Merger(args.remote, default_branch, args.messages, args.reviewer)
    if args.train:
        try:
            merger.train(args.numbers)
        except Held as reason:
            sys.exit(f'train HOLD: {reason}')
    elif held := merger.batch(args.numbers):
        sys.exit(f'needs attention: {" ".join(map(str, held))}')
