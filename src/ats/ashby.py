"""Ashby hosted-jobs adapter."""

from src.ats.base import GenericHTMLAdapter


class AshbyAdapter(GenericHTMLAdapter):
    name = "Ashby"
    host_patterns = ("jobs.ashbyhq.com", "ashbyhq.com")
    page_markers = ("[data-testid='application-form']", "[class*='ashby' i]")
    form_selectors = (
        "[data-testid='application-form']",
        "form[aria-label*='application' i]",
        "form",
    )
    start_selectors = (
        "a[href*='/application']",
        "button:has-text('Apply for this Job')",
        "button:has-text('Apply')",
    )
