"""A config edit cannot change a harness that is already executing."""

import subprocess
from pathlib import Path

from tests.conftest import write_script


def test_replacing_a_harness_leaves_the_running_shell_on_its_original_script(tmp_path: Path) -> None:
    script = write_script(tmp_path / 'harness.sh', 'echo ready\n/bin/cat > /dev/null\necho original\n')
    with subprocess.Popen(
        ['/bin/bash', str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    ) as child:
        assert child.stdout is not None
        assert child.stdout.readline() == 'ready\n'
        write_script(script, 'echo ready\n/bin/cat > /dev/null\necho replaced\n')
        out, _ = child.communicate('\n', timeout=10)
        assert child.returncode == 0
        assert out == 'original\n'
    assert script.read_text().endswith('echo replaced\n')
