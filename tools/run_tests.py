"""Run plugin and management tests in separate interpreters to avoid name clashes."""
import os
from pathlib import Path
import subprocess
import sys


root = Path(__file__).resolve().parents[1]
env = dict(os.environ, COMPANION_TEST_PLUGINS=str(root / 'astrbot-plugins'))
for directory in ('tests', 'management-center'):
    print('Testing ' + directory, flush=True)
    result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', '.', '-p', 'test_*.py', '-q'],
                            cwd=root / directory, env=env)
    if result.returncode:
        raise SystemExit(result.returncode)
