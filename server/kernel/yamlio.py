"""YAML loading with YAML 1.2 booleans.

PyYAML follows YAML 1.1, where bare ``on``, ``off``, ``yes``, ``no`` (any case)
are booleans. State machines use ``on:`` as a key, and event names like ``yes``
are plausible, so every model and library file is read with this loader: only
``true`` / ``false`` are booleans.
"""

from __future__ import annotations

import re
from typing import Any

import yaml


class Yaml12Loader(yaml.SafeLoader):
    pass


# Drop the YAML 1.1 bool resolver and add a 1.2-style one.
Yaml12Loader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
Yaml12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def load_yaml(text: str) -> Any:
    return yaml.load(text, Loader=Yaml12Loader)  # noqa: S506 - a SafeLoader subclass
