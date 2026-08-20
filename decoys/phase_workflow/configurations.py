"""Exact new-path SUP configuration registry. No aliases are accepted."""

from __future__ import annotations


CONFIGURATIONS = {
    "scripted-cpu": {"brain": "scripted", "hardware": "cpu"},
    "mchp-cpu": {"brain": "mchp", "hardware": "cpu"},
    "browseruse-gpu": {"brain": "browseruse", "hardware": "gpu"},
    "smolagents-gpu": {"brain": "smolagents", "hardware": "gpu"},
}


def is_workflow_configuration(config_key: str) -> bool:
    return config_key in CONFIGURATIONS
