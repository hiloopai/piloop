"""The ``piloop`` command line, and the modules for doctor, skills, upgrade and version."""

import sys

from piloop.problems import HomeConfigError


def run() -> None:
    """The ``piloop`` command: ``piloop.cli.main.main``, or one message when the home's settings do not load.

    The command modules load the settings as they import. Without this, a ``config.toml`` that does not load would end
    every command, ``--version`` too, with a traceback.
    """
    try:
        from piloop.cli.main import main  # it loads the settings
    except HomeConfigError as e:
        if sys.argv[1:] == ['--version']:
            from piloop.version import installed

            print(installed())
            return
        raise SystemExit(f'piloop: {e}') from e
    main()
