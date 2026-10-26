"""Pydantic models for Hevy response shapes.

Hevy warns that its API may change, so every model keeps unknown fields and
treats almost every field as optional. Field names keep Hevy's unit suffixes
(``weight_kg``, ``distance_meters``) so that units are never ambiguous.
"""

from pydantic import BaseModel, ConfigDict


class HevyModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class UserInfo(HevyModel):
    id: str | None = None
    username: str | None = None
    name: str | None = None
    url: str | None = None
    weight_unit: str | None = None
    distance_unit: str | None = None
    is_pro: bool | None = None
