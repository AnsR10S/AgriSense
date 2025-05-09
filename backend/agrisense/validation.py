"""Shared exceptions and numeric input validation."""

import math


class AgriSenseError(Exception):
    pass


class ConfigError(AgriSenseError, ValueError):
    pass


class InvalidInputError(AgriSenseError, ValueError):
    pass


class MissingInputError(InvalidInputError):
    pass


class OutOfSeasonError(AgriSenseError):
    pass


def require_number(name, value, low=-math.inf, high=math.inf, unit=""):
    if value is None:
        raise MissingInputError(f"{name} is missing")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidInputError(f"{name} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise InvalidInputError(f"{name} is not finite ({value!r})")
    if not low <= number <= high:
        suffix = f" {unit}" if unit else ""
        raise InvalidInputError(
            f"{name} = {number}{suffix} is outside the plausible range {low} to {high}{suffix}"
        )
    return number
