"""Fake RootlessKit for the native contract tests: marks the environment and runs its child."""

import os
import sys

os.environ["FAKE_ROOTLESSKIT"] = "1"
os.execvp(sys.argv[1], sys.argv[1:])
