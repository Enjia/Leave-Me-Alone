from __future__ import annotations

from typing import Any


VOLATILE_KEYS = {
    "produced_at",
    "timestamp",
    "created_at",
    "updated_at",
    "data_hash",
    "objective_hash",
}


def normalize_artifact_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        normalized: dict[str, Any] = {}
        for key, value in payload.items():
            if key in VOLATILE_KEYS:
                continue
            normalized[key] = normalize_artifact_payload(value)
        return normalized
    if isinstance(payload, list):
        return [normalize_artifact_payload(item) for item in payload]
    return payload
