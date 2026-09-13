from __future__ import annotations

import subprocess
import unicodedata
from typing import TypeGuard


def is_valid_branch_name(value: object) -> TypeGuard[str]:
    """Return whether value has Git branch syntax and no control characters."""
    if not isinstance(value, str) or not value or value.startswith("-"):
        return False
    if any(unicodedata.category(character) == "Cc" for character in value):
        return False
    try:
        result = subprocess.run(
            ["git", "check-ref-format", "--branch", value],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def validate_branch_name(value: object) -> str:
    if not is_valid_branch_name(value):
        raise ValueError("invalid branch name")
    return value
