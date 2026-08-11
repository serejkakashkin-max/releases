"""Presentation-safe release build normalization for special multi-build releases."""

from __future__ import annotations

import re
from typing import Any, Iterable

from services.release_artifact_service import (
    extract_artifact_url,
    extract_distribution_version,
    flatten_artifact_candidates,
    normalize_artifact_url,
)


AI_PLANNER_KE_ID = "14061745"
AI_PLANNER_PARENT_CI = f"CI{AI_PLANNER_KE_ID}"
AI_PLANNER_DPM_BASE = "https://dpm-sigma.sberbank.ru/dpm/front/main/key"

_COMPONENTS = (
    ("e2e_planner", "E2E Planner", "e2e-planner", AI_PLANNER_PARENT_CI),
    (
        "business_plan_builder",
        "JS Business Plan Builder",
        "js-business-plan-builder",
        f"{AI_PLANNER_PARENT_CI}-SUB",
    ),
)


def _ke_digits(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw.startswith("CI"):
        raw = raw[2:]
    digits = re.sub(r"\D", "", raw)
    return digits.lstrip("0") or "0" if digits else ""


def is_ai_planner_ke(value: Any) -> bool:
    return _ke_digits(value) == AI_PLANNER_KE_ID


def _artifact_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(item or "") for item in value.values()).casefold()
    return str(value or "").casefold()


def _component_for(value: Any) -> tuple[str, str, str] | None:
    text = _artifact_text(value)
    for component, label, marker, dpm_key in _COMPONENTS:
        if marker in text:
            return component, label, dpm_key
    return None


def _parent_matches(value: Any) -> bool:
    if not isinstance(value, dict):
        return True
    parent_ci = str(value.get("PARENT_CI") or "").strip()
    return not parent_ci or parent_ci.upper() == AI_PLANNER_PARENT_CI


def resolve_ai_planner_builds(values: Iterable[Any] | Any, *, ke_id: Any) -> dict[str, Any]:
    """Return ordered build metadata for the exact 14061745 release family.

    Other KEs intentionally return ``applies=False`` so their legacy single-build
    selection remains untouched.
    """

    if not is_ai_planner_ke(ke_id):
        return {"applies": False, "builds": [], "ambiguous_components": []}

    by_component: dict[str, list[dict[str, str]]] = {
        component: [] for component, _label, _marker, _dpm_key in _COMPONENTS
    }
    seen: set[tuple[str, str]] = set()

    for source_index, artifact in enumerate(flatten_artifact_candidates(values)):
        if isinstance(artifact, dict) and artifact.get("disabled") is True:
            continue
        if not _parent_matches(artifact):
            continue
        component_info = _component_for(artifact)
        if component_info is None:
            continue
        component, label, dpm_key = component_info
        version = str(extract_distribution_version(artifact) or "").strip()
        artifact_url = normalize_artifact_url(extract_artifact_url(artifact))
        if not version:
            continue
        identity = (component, version.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        by_component[component].append({
            "component": component,
            "label": label,
            "version": version,
            "artifact_url": artifact_url,
            "dpm_url": f"{AI_PLANNER_DPM_BASE}/{dpm_key}",
            "source_index": source_index,
        })

    builds: list[dict[str, str]] = []
    ambiguous_components: list[str] = []
    for component, _label, _marker, _dpm_key in _COMPONENTS:
        candidates = by_component[component]
        if len(candidates) > 1:
            ambiguous_components.append(component)
        builds.extend(candidates)

    for build in builds:
        build.pop("source_index", None)
    return {
        "applies": True,
        "builds": builds,
        "ambiguous_components": ambiguous_components,
    }


def normalize_release_builds(item: dict[str, Any]) -> list[dict[str, str]]:
    """Hydrate the new list contract from legacy singular snapshot fields."""

    raw_builds = item.get("release_builds")
    if isinstance(raw_builds, list):
        normalized = []
        for raw in raw_builds:
            if not isinstance(raw, dict):
                continue
            version = str(raw.get("version") or "").strip()
            if not version:
                continue
            normalized.append({
                "component": str(raw.get("component") or "legacy").strip() or "legacy",
                "label": str(raw.get("label") or "Сборка").strip() or "Сборка",
                "version": version,
                "artifact_url": str(raw.get("artifact_url") or "").strip(),
                "dpm_url": str(raw.get("dpm_url") or "").strip(),
            })
        if normalized:
            return normalized

    version = str(item.get("release_version") or "").strip()
    if not version:
        return []
    component = "legacy"
    label = "Сборка"
    dpm_url = ""
    if is_ai_planner_ke(item.get("ke_id")):
        component_info = _component_for({
            "version": version,
            "url": item.get("release_dist_url") or "",
        })
        if component_info:
            component, label, dpm_key = component_info
            dpm_url = f"{AI_PLANNER_DPM_BASE}/{dpm_key}"
    return [{
        "component": component,
        "label": label,
        "version": version,
        "artifact_url": str(item.get("release_dist_url") or "").strip(),
        "dpm_url": dpm_url,
    }]
