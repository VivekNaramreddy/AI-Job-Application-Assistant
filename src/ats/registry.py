"""Ordered ATS adapter detection without platform conditionals in the workflow."""

from typing import List, Type
from urllib.parse import urlsplit

from playwright.async_api import Page

from src.ats.ashby import AshbyAdapter
from src.ats.base import ATSAdapter, GenericHTMLAdapter, LogFunction
from src.ats.greenhouse import GreenhouseAdapter
from src.ats.lever import LeverAdapter
from src.ats.workday import WorkdayAdapter


class AdapterRegistry:
    adapter_types: List[Type[ATSAdapter]] = [
        GreenhouseAdapter,
        LeverAdapter,
        AshbyAdapter,
        WorkdayAdapter,
        GenericHTMLAdapter,
    ]

    @classmethod
    def provider_from_url(cls, url: str) -> str:
        """Identify a known provider from listing metadata without visiting it."""
        host = (urlsplit(url).hostname or "").lower()
        return next((adapter.name for adapter in cls.adapter_types
                     if any(host == pattern or host.endswith("." + pattern)
                            for pattern in adapter.host_patterns)), "")

    @classmethod
    async def create(cls, page: Page, log: LogFunction) -> ATSAdapter:
        for adapter_type in cls.adapter_types:
            if await adapter_type.detect(page, page.url):
                return adapter_type(page, log)
        return GenericHTMLAdapter(page, log)
