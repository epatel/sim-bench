"""Model definition: the static description of entities and connections.

This is what gets saved as YAML/JSON and edited by the canvas or the agent.
The kernel builds runtime objects from it (see ``sim.Simulation``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .yamlio import load_yaml


class PortRef(BaseModel):
    entity: str
    port: str

    @classmethod
    def parse(cls, ref: str) -> PortRef:
        entity, sep, port = ref.rpartition(".")
        if not sep or not entity or not port:
            raise ValueError(f"port reference {ref!r} must look like 'entity.port'")
        return cls(entity=entity, port=port)

    def __str__(self) -> str:
        return f"{self.entity}.{self.port}"


class EntitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    name: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    # Canvas position; ignored by the kernel.
    pos: tuple[float, float] | None = None
    # Template instance this entity came from (drawn as a frame; a composite from M4).
    group: str | None = None

    @field_validator("id")
    @classmethod
    def _id_has_no_dot(cls, v: str) -> str:
        if "." in v or not v:
            raise ValueError(f"entity id {v!r} must be non-empty and contain no '.'")
        return v


class ConnectionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str | None = None
    source: str = Field(alias="from")
    target: str = Field(alias="to")
    latency: Any = 0.0  # distribution spec
    bandwidth: float | None = None  # size units per time unit; None = infinite
    loss: float = 0.0  # probability a message is dropped
    ordered: bool = True  # never deliver out of send order
    up: bool = True

    @property
    def src(self) -> PortRef:
        return PortRef.parse(self.source)

    @property
    def dst(self) -> PortRef:
        return PortRef.parse(self.target)

    def key(self) -> str:
        return self.id or f"{self.source}->{self.target}"


class AbstractSpec(BaseModel):
    """The single entity that stands in for a group in abstract mode."""

    model_config = ConfigDict(extra="forbid")

    type: str
    params: dict[str, Any] = Field(default_factory=dict)
    # Where the numbers came from (set by calibration); informational only.
    calibration: dict[str, Any] | None = None


class GroupSpec(BaseModel):
    """A composite: the entities whose ``group`` is this id, seen from outside.

    ``inputs`` / ``outputs`` map boundary port names to inner ports
    ("entity.port"). Outside connections may target either the inner port or
    ``<group id>.<boundary port>``. In ``abstract`` mode the members are
    replaced by one entity of type ``abstract.type`` whose ports must include
    the boundary port names.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str | None = None
    template: str | None = None  # template it was created from, if any
    inputs: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    mode: Literal["expanded", "abstract"] = "expanded"
    abstract: AbstractSpec | None = None
    pos: tuple[float, float] | None = None  # collapsed node position on the canvas

    @field_validator("id")
    @classmethod
    def _id_has_no_dot(cls, v: str) -> str:
        if "." in v or not v:
            raise ValueError(f"group id {v!r} must be non-empty and contain no '.'")
        return v


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "untitled"
    description: str = ""
    entities: list[EntitySpec] = Field(default_factory=list)
    connections: list[ConnectionSpec] = Field(default_factory=list)
    groups: list[GroupSpec] = Field(default_factory=list)

    def entity(self, entity_id: str) -> EntitySpec:
        for e in self.entities:
            if e.id == entity_id:
                return e
        raise KeyError(entity_id)


def load_model(path: str | Path) -> ModelSpec:
    data = load_yaml(Path(path).read_text())
    return ModelSpec.model_validate(data)
