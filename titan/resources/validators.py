from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from titan.resources import Resource


def coerce_from_str(cls: "Resource") -> callable:
    def _coerce(name_or_resource):
        if isinstance(name_or_resource, str):
            return cls(name=name_or_resource, stub=True)
        else:
            return name_or_resource

    return _coerce


def serialize_as_named_resource(name_or_resource) -> "Resource":
    if isinstance(name_or_resource, str):
        return name_or_resource
    else:
        res = name_or_resource
        return f"{res.resource_type} {res.fqn}"
