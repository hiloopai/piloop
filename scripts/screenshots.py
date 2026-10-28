"""Render ``piloop watch`` screens of a real campaign as SVG files, for the docs.

    uv run python scripts/screenshots.py <PILOOP_HOME> <campaign> <out_dir>

It drives the real app headless (Textual's test pilot) against the daemon of the home, on its socket, and saves what
each screen shows. It only reads: it presses navigation keys, never a key that writes.
"""

import asyncio
import sys
from pathlib import Path

from piloop.actor import System
from piloop.api.client import socket_http
from piloop.settings import Settings, WatchSettings
from piloop.watch.app import Watch
from piloop.watch.panels import Api

SIZE = (160, 46)
SHOTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('watch-campaign', ()),
    ('watch-candidates', ('4',)),
    ('watch-ideas', ('3',)),
)


async def shoot(api: Api, campaign: str, out: Path) -> None:
    """Open the campaign once per shot, press its keys, and save the screen."""
    for name, keys in SHOTS:
        app = Watch(api, campaign, System.HUMAN.signature, WatchSettings())
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause(0.5)
            for key in keys:
                await pilot.press(key)
                await pilot.pause(0.3)
            app.save_screenshot(filename=f'{name}.svg', path=str(out))
        print(out / f'{name}.svg')


def main() -> None:
    """Parse the arguments and render every shot."""
    home, campaign, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)
    with socket_http(Settings(home=home).socket_file, System.HUMAN) as http:
        asyncio.run(shoot(Api.over(http), campaign, out))


if __name__ == '__main__':
    main()
