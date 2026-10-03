"""Lever hosted-jobs adapter."""

from src.ats.base import GenericHTMLAdapter


class LeverAdapter(GenericHTMLAdapter):
    name = "Lever"
    host_patterns = ("jobs.lever.co", "jobs.eu.lever.co")
    page_markers = (".lever", "form.application-form")
    form_selectors = ("form.application-form", "#application-form", "form")
    start_selectors = (
        "a.postings-btn[href*='/apply']",
        "a:has-text('Apply for this job')",
        "a:has-text('Apply now')",
    )
