"""Feature state primitive stored in repo-local features.yaml."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import yaml


FEATURE_STATES = {"not_started", "active", "blocked", "passing"}


@dataclass
class Feature:
    id: str
    behavior: str
    verification: str
    state: str = "not_started"
    evidence: str = ""
    blocked_reason: str = ""

    def validate(self) -> None:
        if not self.id.strip():
            raise ValueError("feature id cannot be empty")
        if not self.behavior.strip():
            raise ValueError("feature behavior cannot be empty")
        if not self.verification.strip():
            raise ValueError("feature verification command cannot be empty")
        if self.state not in FEATURE_STATES:
            raise ValueError(f"invalid feature state: {self.state}")


def feature_file(cwd: Optional[Path] = None) -> Path:
    return (cwd or Path.cwd()) / "features.yaml"


def load_features(cwd: Optional[Path] = None) -> list[Feature]:
    fpath = feature_file(cwd)
    if not fpath.exists():
        return []
    raw = yaml.safe_load(fpath.read_text()) or {}
    rows = raw.get("features", [])
    features: list[Feature] = []
    for row in rows:
        feat = Feature(
            id=str(row.get("id", "")),
            behavior=str(row.get("behavior", "")),
            verification=str(row.get("verification", "")),
            state=str(row.get("state", "not_started")),
            evidence=str(row.get("evidence", "")),
            blocked_reason=str(row.get("blocked_reason", "")),
        )
        feat.validate()
        features.append(feat)
    return features


def save_features(features: list[Feature], cwd: Optional[Path] = None) -> None:
    for feat in features:
        feat.validate()
    fpath = feature_file(cwd)
    payload = {"features": [asdict(f) for f in features]}
    fpath.write_text(yaml.safe_dump(payload, sort_keys=False))


def get_feature(feature_id: str, cwd: Optional[Path] = None) -> Optional[Feature]:
    for feat in load_features(cwd):
        if feat.id == feature_id:
            return feat
    return None


def get_active_feature(cwd: Optional[Path] = None) -> Optional[Feature]:
    for feat in load_features(cwd):
        if feat.state == "active":
            return feat
    return None
