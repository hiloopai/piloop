"""Install the Litestream that the tests run into the environment's ``bin``, checked against its release's SHA256.

    uv run -m scripts.litestream

The ``bucket`` leader runs Litestream as a child (``piloop.plugins.leader.bucket``), and ``tests/leader/test_litestream.py``
runs the real one. ``make install`` and CI run this. An installed copy of the pinned version is kept.
"""

import hashlib
import io
import platform
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = '0.5.17'
PLATFORMS = {
    ('Linux', 'x86_64'): ('linux-x86_64', 'cfb371176d164437ae869f8351cfde49bd1804ae71c61923f75c9cba9c9c006d'),
    ('Linux', 'aarch64'): ('linux-arm64', 'f8ca4a050095c1efbda2c4365172e61bf9d955ea0d9ac42f448b52e51819baa5'),
    ('Darwin', 'arm64'): ('darwin-arm64', 'e211f68ff7658d19f193f2914417afdf8f89a053ff8f263e5d6b3b1d3bbc7b08'),
    ('Darwin', 'x86_64'): ('darwin-x86_64', '891875af09db152e93a4b31a8a79f538ce7ce702c132803cfe0a831e7cb1b7db'),
}
"""Each release asset and its SHA256 in the release's ``checksums.txt``, by ``platform.system()`` and ``machine()``."""


def installed(target: Path) -> bool:
    """Whether ``target`` is the pinned version."""
    if not target.is_file():
        return False
    done = subprocess.run([target, 'version'], capture_output=True, text=True, check=False)
    return done.returncode == 0 and done.stdout.strip() == VERSION


def main() -> None:
    """Install the pinned Litestream into the environment's ``bin``, unless it is there."""
    target = Path(sys.prefix) / 'bin' / 'litestream'
    if installed(target):
        print(f'Litestream {VERSION} is at {target}')
        return
    system, machine = platform.system(), platform.machine()
    if (system, machine) not in PLATFORMS:
        raise SystemExit(f'no pinned Litestream {VERSION} for {system} {machine}: add its checksum to {__file__}')
    asset, sha256 = PLATFORMS[system, machine]
    name = f'litestream-{VERSION}-{asset}.tar.gz'
    url = f'https://github.com/benbjohnson/litestream/releases/download/v{VERSION}/{name}'
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read()
    got = hashlib.sha256(archive).hexdigest()
    if got != sha256:
        raise SystemExit(f'{url}: SHA256 {got}, not the pinned {sha256}')
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        member = tar.extractfile('litestream')
        if member is None:
            raise SystemExit(f'{url} holds no litestream executable')
        target.write_bytes(member.read())
    target.chmod(0o755)
    print(f'Litestream {VERSION} installed at {target}')


if __name__ == '__main__':
    main()
