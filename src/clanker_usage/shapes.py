"""The structure of a provider response, without its values.

A response's shape is the set of its paths and value types, such as
`$.data.limits[].unit: number`. Recorded shapes are synthetic copies of real
responses, kept in shapes/<provider>/, and `check` reports any path a live
response has that no recorded shape has.
"""

import json
import re
from datetime import datetime
from importlib.resources import files


def kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def paths(value, path="$"):
    """Every leaf path in value with its type, as "path: type" strings."""
    if isinstance(value, dict):
        if not value:
            return {f"{path}: object"}
        return set().union(*(paths(item, f"{path}.{key}") for key, item in value.items()))
    if isinstance(value, list):
        if not value:
            return {f"{path}: list"}
        return set().union(*(paths(item, f"{path}[]") for item in value))
    return {f"{path}: {kind(value)}"}


def synthetic(value, keep, key=None):
    """A copy of value with every scalar replaced by a fixed one of the same type, except under keys in keep.

    keep names keys whose values describe the format, such as a window's unit,
    rather than the account.
    """
    if isinstance(value, dict):
        return {name: synthetic(item, keep, name) for name, item in value.items()}
    if isinstance(value, list):
        items = []
        for item in value:
            # Identical synthetic items, such as a list of model names, add nothing to the shape.
            if (copy := synthetic(item, keep, key)) not in items:
                items.append(copy)
        return items
    if key in keep or value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return 1
    if isinstance(value, float):
        return 0.5
    if isinstance(value, str):
        return _synthetic_string(value)
    raise TypeError(f"unexpected JSON value of type {type(value).__name__}")


def _synthetic_string(value):
    """A fixed string in the same format, so numbers and times sent as strings still parse."""
    if re.fullmatch(r"-?\d+(\.\d+)?", value):
        # Same sign and number of digits, such as a Unix time or a signed balance in micros.
        return re.sub(r"\d", "0", value).replace("0", "1", 1)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "synthetic"
    return "2001-01-01T00:00:00" + ("Z" if value.endswith("Z") else "+00:00")


def recorded(provider):
    """The recorded synthetic responses of a provider, by file name."""
    folder = files("clanker_usage") / "shapes" / provider
    if not folder.is_dir():
        return {}
    return {item.name: json.loads(item.read_text()) for item in sorted(folder.iterdir(), key=lambda item: item.name)
            if item.name.endswith(".json")}


def known_paths(provider):
    return set().union(*(paths(response) for response in recorded(provider).values()))
