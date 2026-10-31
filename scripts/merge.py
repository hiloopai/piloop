"""Merge approved pull requests, each as one squash commit, once a full CI run of its head is green.

    uv run -m scripts.merge --repo OWNER/NAME --remote REMOTE --messages DIR 590 591
    uv run -m scripts.merge --repo OWNER/NAME --remote REMOTE --messages DIR --train 577 578 579

Each pull request merges in the order given. One that fails, conflicts or is held is reported and skipped, and the rest
of the batch goes on. ``--train`` lands a stack, bottom first, in one push of one squash commit per pull request, from
the green run of its top; the lower pull requests are closed with the commit that landed them.

``DIR`` holds the squash messages: ``titles.txt`` has a ``N|title`` line per pull request and ``N.md`` its body.
``names.txt``, if any, lists words that no squash message may contain, one per line. ``acked-comments.txt`` lists the
ids of review comments by ``--reviewer`` that were answered elsewhere. ``REMOTE`` must be the git remote of
``OWNER/NAME``: the script refuses to start otherwise, so a train never pushes to another repository's main.
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
from datetime import UTC, datetime
from pathlib import Path

FULL_SUITE = 'coverage'
"""The CI job that runs only when the whole suite ran, and fails under 100% coverage: the merge requires it."""
GATE = 'check'
"""The CI job that is green when every other job of the run is."""
DO_NOT_MERGE = 'do-not-merge'
"""The label of a pull request that waits for a review, such as one that deletes tests."""
RUN_START = 60
"""Seconds to wait for the CI run that ``gh pr ready`` starts to show up."""
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

    def conclusions(self, sha: str) -> dict[str, str]:
        """The latest conclusion of each check of a commit, by name; an unfinished one is ``pending``."""
        runs = json.loads(gh('api', '--paginate', '--slurp', f'repos/{{owner}}/{{repo}}/commits/{sha}/check-runs'))
        latest = sorted((r for page in runs for r in page['check_runs']), key=lambda r: r['id'])
        return {r['name']: r['conclusion'] or 'pending' for r in latest}

    def start_full_suite(self, pr: PullRequest) -> str | None:
        """A draft runs the tests that its diff affects; marked ready, it runs the whole suite. The time it starts."""
        if not pr.draft or self.conclusions(pr.head).get(FULL_SUITE) == 'success':
            return None
        since = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
        gh('pr', 'ready', str(pr.number))
        return since

    def watch(self, pr: PullRequest, since: str | None) -> None:
        """Wait for the CI runs of the head, and for the one started ``since``; hold unless its full suite is green."""
        deadline = time.monotonic() + RUN_START
        while True:
            runs = json.loads(gh('run', 'list', '--commit', pr.head, '--json', 'databaseId,status,createdAt'))
            if since is None or any(r['createdAt'] >= since for r in runs) or time.monotonic() > deadline:
                break
            time.sleep(5)
        for entry in runs:
            if entry['status'] != 'completed':
                subprocess.run(['gh', 'run', 'watch', str(entry['databaseId'])], capture_output=True)
        found = self.conclusions(pr.head)
        for job in (GATE, FULL_SUITE):
            if found.get(job) != 'success':
                raise Held(f'{job} is {found.get(job, "missing")} at {pr.head[:8]}')

    def message(self, number: int) -> tuple[str, str]:
        titles = (self.messages / 'titles.txt').read_text().splitlines()
        title = next((line.split('|', 1)[1] for line in titles if line.startswith(f'{number}|')), None)
        body = self.messages / f'{number}.md'
        if title is None or not body.is_file():
            raise Held(f'no title in titles.txt or no {body.name}')
        names = self.messages / 'names.txt'
        words = [w for w in names.read_text().split() if w] if names.is_file() else []
        text = f'{title}\n{body.read_text()}'
        if words and re.search(rf'\b({"|".join(map(re.escape, words))})\b', text, re.IGNORECASE):
            raise Held('its squash message has a word of names.txt')
        return f'{title} (#{number})', body.read_text()

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

    def merged_tree(self, main: str, head: str) -> str:
        result = subprocess.run(['git', 'merge-tree', '--write-tree', main, head], capture_output=True, text=True)
        if result.returncode:
            raise Held(f'conflicts with main at {head[:8]}')
        return result.stdout.split()[0]

    def merge(self, number: int, since: str | None) -> str:
        pr = self.pull(number)
        self.unanswered(number)
        subject, body = self.message(number)
        self.watch(pr, since)
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
        started: dict[int, str | None] = {}
        for number in numbers:
            try:
                started[number] = self.start_full_suite(self.pull(number))
            except Held as reason:
                print(f'{number} HOLD: {reason}')
                held.append(number)
        for number, since in started.items():
            try:
                print(f'MERGED {number} {self.merge(number, since)}')
            except Held as reason:
                print(f'{number} HOLD: {reason}')
                held.append(number)
        return held

    def train(self, numbers: list[int]) -> str:
        """Land a stack, bottom first, as one squash commit per pull request, in one push; the commit is returned."""
        prs = [self.pull(n) for n in numbers]
        main = self.main()
        for below, above in zip(prs, prs[1:], strict=False):
            if above.base_ref != below.head_ref:
                raise Held(f'#{above.number} is based on {above.base_ref}, not on #{below.number}')
        for below, above in zip([main, *(pr.head for pr in prs)], (pr.head for pr in prs), strict=False):
            if subprocess.run(['git', 'merge-base', '--is-ancestor', below, above]).returncode:
                raise Held(f'the stack is not on the tip of main at {above[:8]}: rebase it')
        messages = []
        for pr in prs:
            self.unanswered(pr.number)
            messages.append(self.message(pr.number))
        top = prs[-1]
        self.watch(top, self.start_full_suite(top))
        check_migrations(run('git', 'rev-parse', f'{top.head}^{{tree}}'))
        squashed: list[str] = []
        for pr, (subject, body) in zip(prs, messages, strict=True):
            tree = run('git', 'rev-parse', f'{pr.head}^{{tree}}')
            squashed.append(run('git', 'commit-tree', tree, '-p', (squashed or [main])[-1], '-m', subject, '-m', body))
        parent = squashed[-1]
        pushed = subprocess.run(
            ['git', 'push', '-q', self.remote, f'{parent}:refs/heads/{self.branch}'], capture_output=True
        )
        if pushed.returncode:
            raise Held(f'main moved before the push: {pushed.stderr.decode().strip()}')
        for pr, landed in zip(prs, squashed, strict=True):
            gh(
                'pr',
                'close',
                str(pr.number),
                '--comment',
                f'Landed on main as {landed}, in the train of #{top.number}.',
            )
        return parent


def run(*args: str) -> str:
    """The output of a command that must succeed."""
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def gh(*args: str) -> str:
    """The output of a ``gh`` command, in the repository that ``gh`` picks."""
    return run('gh', *args)


def check_remote(remote: str, repo: str) -> None:
    """Hold unless the remote's push URL is the GitHub repository ``OWNER/NAME`` that ``gh`` merges in.

    A train pushes to the remote's main: a remote of another repository would publish there.
    """
    url = run('git', 'remote', 'get-url', '--push', remote)
    match = GITHUB_URL.fullmatch(url)
    if match is None or match['repo'].lower() != repo.lower():
        raise Held(f'the remote {remote} is {url}, not the repository {repo}')


def check_migrations(tree: str) -> None:
    """Each migrations directory of a tree declares each revision id once and has one head; holds if not."""
    found = subprocess.run(
        ['git', 'grep', '-E', '^(down_)?revision\\b', tree, '--', 'piloop/**/migrations/**.py'],
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
            print(f'LANDED {" ".join(map(str, args.numbers))} {merger.train(args.numbers)}')
        except Held as reason:
            sys.exit(f'train HOLD: {reason}')
    elif held := merger.batch(args.numbers):
        sys.exit(f'needs attention: {" ".join(map(str, held))}')
