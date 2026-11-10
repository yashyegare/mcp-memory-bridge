import subprocess
import sys

from conftest import ROOT


def test_forced_termination_recovers_in_both_durability_modes():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "crash_consistency.py"), "--rounds", "2"],
        capture_output=True, text=True, timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NORMAL round 2/2: integrity, chain, replay" in result.stdout
    assert "FULL round 2/2: integrity, chain, replay" in result.stdout
