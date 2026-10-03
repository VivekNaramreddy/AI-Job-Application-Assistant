import unittest

from playwright.async_api import async_playwright

from src.ats.ashby import AshbyAdapter
from src.ats.base import ATSAdapter, GenericHTMLAdapter
from src.ats.greenhouse import GreenhouseAdapter
from src.ats.lever import LeverAdapter
from src.ats.workday import WorkdayAdapter


async def no_log(message):
    return None


class ATSAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True)
        except Exception as exc:
            self.skipTest(f"Local Chromium is unavailable: {exc}")
        self.page = await self.browser.new_page()
        await self.page.set_content("<main>Job application</main>")

    async def asyncTearDown(self):
        if hasattr(self, "browser"):
            await self.browser.close()
        if hasattr(self, "playwright"):
            await self.playwright.stop()

    async def test_platform_detection_by_host(self):
        cases = (
            (GreenhouseAdapter, "https://boards.greenhouse.io/example/jobs/1"),
            (LeverAdapter, "https://jobs.lever.co/example/1"),
            (AshbyAdapter, "https://jobs.ashbyhq.com/example/1"),
            (WorkdayAdapter, "https://example.wd5.myworkdayjobs.com/jobs/1"),
        )
        for adapter_type, url in cases:
            with self.subTest(adapter=adapter_type.name):
                self.assertTrue(await adapter_type.detect(self.page, url))

    def test_interface_exposes_required_operations(self):
        for method in (
            "detect", "inspect", "extract_fields", "fill_fields", "validate",
            "next_step", "is_final_step", "submit",
        ):
            self.assertTrue(hasattr(ATSAdapter, method))

    async def test_generic_is_fallback(self):
        self.assertTrue(
            await GenericHTMLAdapter.detect(
                self.page, "https://careers.example.test/jobs/1"
            )
        )


if __name__ == "__main__":
    unittest.main()
