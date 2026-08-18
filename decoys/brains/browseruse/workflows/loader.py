"""
Workflow loader for BrowserUse.

Loads BrowserUse-native workflows (browse_web, web_search, browse_youtube).
All workflows use BrowserUse's Playwright-based agent - no cross-brain imports.
"""
from typing import List

from brains.browseruse.workflows.base import BUWorkflow
from brains.browseruse.prompts import BUPrompts


def load_workflows(
    model: str = None,
    prompts: BUPrompts = None,
    headless: bool = True,
    max_steps: int = 10,
    enable_whois: bool = True,
    enable_download: bool = True,
    enabled_workflows=None,
) -> List[BUWorkflow]:
    """
    Load BrowserUse-native workflows.

    Always loads the three Playwright-driven content workflows
    (browse_web, web_search, browse_youtube). The behavior-driven
    workflows (whois_lookup, download_files) are gated per-flag from
    behavior.json: enable_whois / enable_download. PHASE's dumb_baseline
    emits both as false to disable; PHASE feedback proper emits true.

    Args:
        model: Model name for workflows
        prompts: Prompts configuration
        headless: Run browser in headless mode
        max_steps: Maximum steps per task
        enable_whois: Register whois_lookup workflow (default True)
        enable_download: Register download_files workflow (default True)

    Returns:
        List of BUWorkflow instances
    """
    from brains.browseruse.workflows.browse_web import load as load_browse_web
    from brains.browseruse.workflows.web_search import load as load_web_search
    from brains.browseruse.workflows.browse_youtube import load as load_browse_youtube

    enabled = set(enabled_workflows) if enabled_workflows is not None else None
    workflows = []
    if enabled is None or "BrowseWeb" in enabled:
        workflows.append(load_browse_web(
            model=model, prompts=prompts, headless=headless, max_steps=max_steps))
    if enabled is None or "WebSearch" in enabled:
        workflows.append(load_web_search(
            model=model, prompts=prompts, headless=headless, max_steps=max_steps))
    if enabled is None or "BrowseYouTube" in enabled:
        workflows.append(load_browse_youtube(
            model=model, prompts=prompts, headless=headless, max_steps=max_steps))

    if enable_whois and (enabled is None or "WhoisLookup" in enabled):
        from brains.browseruse.workflows.whois_lookup import load as load_whois
        workflows.append(load_whois(model=model))
    if enable_download and (enabled is None or "DownloadFiles" in enabled):
        from brains.browseruse.workflows.download_files import load as load_download
        workflows.append(load_download(model=model))

    return workflows
