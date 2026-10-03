"""Greenhouse adapter using its stable application-form conventions."""

from src.ats.base import GenericHTMLAdapter


class GreenhouseAdapter(GenericHTMLAdapter):
    name = "Greenhouse"
    host_patterns = ("greenhouse.io", "greenhouse.com")
    page_markers = ("#grnhse_app", "[data-mapped='greenhouse']")
    form_selectors = (
        "#application-form",
        "#grnhse_app form",
        "form[action*='greenhouse' i]",
    )
    start_selectors = (
        "a[href='#application-form']",
        "a:has-text('Apply for this job')",
        "button:has-text('Apply for this job')",
    )
