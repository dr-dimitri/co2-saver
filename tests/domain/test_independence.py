# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Architecture tests for the pure accounting domain."""

import subprocess
import sys
from pathlib import Path


def test_domain_import_path_has_no_home_assistant_dependency() -> None:
    """Import the full domain graph without site packages such as Home Assistant."""
    repository = Path(__file__).parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import custom_components.co2saver.domain.storage",
        ],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
