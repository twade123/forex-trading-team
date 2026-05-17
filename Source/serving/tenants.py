"""Tenant resolution — request → which tenant lane.

Resolution order:
  1. X-Jarvis-Tenant header (explicit)
  2. Model name prefix: 'trading/...' → trading; 'boardroom/...' → boardroom
  3. Fallback to 'default' tenant
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

import yaml

logger = logging.getLogger("serving.tenants")


@dataclass(frozen=True)
class Tenant:
    name: str
    priority: int
    description: str = ""


@lru_cache(maxsize=4)
def _load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def _build_tenant(name: str, cfg: dict) -> Tenant:
    return Tenant(
        name=name,
        priority=int(cfg.get("priority", 7)),
        description=cfg.get("description", ""),
    )


def resolve(headers: dict, body: dict, config_path: str) -> Tenant:
    """Resolve a tenant from an incoming request."""
    cfg = _load_config(config_path)
    tenants_cfg = cfg.get("tenants") or {}

    # Normalize header keys to lowercase
    norm_headers = {k.lower(): v for k, v in (headers or {}).items()}
    explicit = norm_headers.get("x-jarvis-tenant")

    if explicit:
        if explicit in tenants_cfg:
            return _build_tenant(explicit, tenants_cfg[explicit])
        logger.warning("Unknown tenant '%s' from X-Jarvis-Tenant — falling back to default", explicit)

    # Model prefix routing: 'trading/...' or 'boardroom/...'
    model = (body or {}).get("model", "")
    if isinstance(model, str) and "/" in model:
        prefix = model.split("/", 1)[0]
        if prefix in tenants_cfg:
            return _build_tenant(prefix, tenants_cfg[prefix])

    # Fallback
    if "default" in tenants_cfg:
        return _build_tenant("default", tenants_cfg["default"])
    return Tenant(name="default", priority=7)
