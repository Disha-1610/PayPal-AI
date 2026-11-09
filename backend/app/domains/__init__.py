from app.domains.a2a import A2A
from app.domains.base import DomainPlugin
from app.domains.contractor import CONTRACTOR
from app.domains.shopping import SHOPPING

_REGISTRY: dict[str, DomainPlugin] = {p.name: p for p in (SHOPPING, CONTRACTOR, A2A)}


def get_plugin(name: str) -> DomainPlugin:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown domain {name!r}; available: {sorted(_REGISTRY)}") from None
