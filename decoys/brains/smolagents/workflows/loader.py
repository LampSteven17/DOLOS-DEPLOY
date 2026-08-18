"""
Workflow loader for SmolAgents.

Loads SmolAgents-native workflows. Always loads the three LLM-driven
content workflows (browse_web, web_search, browse_youtube). The
behavior-driven workflows (whois_lookup, download_files) are gated
per-flag from behavior.json: enable_whois / enable_download. PHASE's
dumb_baseline emits both as false to disable; PHASE feedback proper
emits true.
"""
from typing import List

from brains.smolagents.workflows.base import SmolWorkflow


def load_workflows(model: str = None, prompts=None,
                   enable_whois: bool = True,
                   enable_download: bool = True,
                   enabled_workflows=None) -> List[SmolWorkflow]:
    """
    Load SmolAgents-native workflows.

    Args:
        model: Model name for workflows
        prompts: Prompts configuration
        enable_whois: Register whois_lookup workflow (default True;
                      behavior.enable_whois=false in dumb_baseline mode)
        enable_download: Register download_files workflow (default True;
                         behavior.enable_download=false in dumb_baseline)

    Returns:
        List of SmolWorkflow instances
    """
    from brains.smolagents.workflows.browse_web import load as load_browse_web
    from brains.smolagents.workflows.web_search import load as load_web_search
    from brains.smolagents.workflows.browse_youtube import load as load_browse_youtube

    enabled = set(enabled_workflows) if enabled_workflows is not None else None
    workflows = []
    if enabled is None or "BrowseWeb" in enabled:
        workflows.append(load_browse_web(model=model, prompts=prompts))
    if enabled is None or "WebSearch" in enabled:
        workflows.append(load_web_search(model=model, prompts=prompts))
    if enabled is None or "BrowseYouTube" in enabled:
        workflows.append(load_browse_youtube(model=model, prompts=prompts))

    if enable_whois and (enabled is None or "WhoisLookup" in enabled):
        from brains.smolagents.workflows.whois_lookup import load as load_whois
        workflows.append(load_whois(model=model))
    if enable_download and (enabled is None or "DownloadFiles" in enabled):
        from brains.smolagents.workflows.download_files import load as load_download
        workflows.append(load_download(model=model))

    return workflows
