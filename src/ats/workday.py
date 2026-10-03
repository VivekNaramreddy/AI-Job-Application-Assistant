"""Workday adapter for accessible/basic application steps."""

from src.ats.base import GenericHTMLAdapter


class WorkdayAdapter(GenericHTMLAdapter):
    name = "Workday"
    host_patterns = ("myworkdayjobs.com", "myworkday.com")
    page_markers = ("[data-automation-id='applicationForm']", "[data-automation-id='jobPostingPage']")
    form_selectors = (
        "[data-automation-id='applicationForm']",
        "form[data-automation-id]",
        "form",
    )
    start_selectors = (
        "a[data-automation-id='applyButton']",
        "button[data-automation-id='applyButton']",
        "button:has-text('Apply')",
    )
    next_selectors = (
        "button[data-automation-id='bottom-navigation-next-button']",
        "button:has-text('Save and Continue')",
        "button:has-text('Next')",
    )
    submit_selectors = (
        "button[data-automation-id='bottom-navigation-next-button']:has-text('Submit')",
        "button:has-text('Submit application')",
        "button:has-text('Submit')",
    )
