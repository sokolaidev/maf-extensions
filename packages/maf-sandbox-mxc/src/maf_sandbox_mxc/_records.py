"""Type guards for dynamic records before their protocol-specific validation."""

from typing import Any, TypeGuard


def is_record(value: object) -> TypeGuard[dict[Any, Any]]:
    """Recognize a dictionary without trusting its field names or values."""
    return isinstance(value, dict)


def is_sequence(value: object) -> TypeGuard[list[Any]]:
    """Recognize a JSON array before validating its members and bounds."""
    return isinstance(value, list)
