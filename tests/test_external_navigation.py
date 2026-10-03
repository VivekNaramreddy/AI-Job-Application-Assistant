import unittest

from playwright.async_api import async_playwright

from src.external_navigation import ExternalApplicationLauncher


LINKEDIN_URL = "https://www.linkedin.com/jobs/view/1234567890/"
ATS_URL = "https://careers.example.test/apply/123"


class ExternalNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True)
        except Exception as exc:
            self.skipTest(f"Local Chromium is unavailable: {exc}")
        self.context = await self.browser.new_context()

        async def route_handler(route):
            url = route.request.url
            if "linkedin.com" in url:
                await route.fulfill(
                    content_type="text/html",
                    body=(
                        "<button class='jobs-apply-button' "
                        f"onclick=\"location.href='{ATS_URL}'\">Apply</button>"
                    ),
                )
            elif url == ATS_URL:
                await route.fulfill(
                    content_type="text/html", body="<form><input name='email'></form>"
                )
            else:
                await route.fulfill(content_type="text/html", body="<p>final</p>")

        await self.context.route("**/*", route_handler)

    async def asyncTearDown(self):
        if hasattr(self, "browser"):
            await self.browser.close()
        if hasattr(self, "playwright"):
            await self.playwright.stop()

    async def test_same_tab_redirect_uses_dedicated_page_and_preserves_linkedin(self):
        linkedin = await self.context.new_page()
        await linkedin.goto(LINKEDIN_URL)
        control = linkedin.locator("button.jobs-apply-button")

        session = await ExternalApplicationLauncher.open(linkedin, control)

        self.assertEqual(linkedin.url, LINKEDIN_URL)
        self.assertEqual(session.application_page.url, ATS_URL)
        self.assertIsNot(session.application_page, linkedin)
        await session.close()

    async def test_popup_is_captured_without_replacing_linkedin_page(self):
        async def popup_linkedin(route):
            await route.fulfill(
                content_type="text/html",
                body=(
                    "<button class='jobs-apply-button' "
                    f"onclick=\"window.open('{ATS_URL}', '_blank')\">Apply</button>"
                ),
            )

        await self.context.unroute("**/*")
        await self.context.route("**linkedin.com/**", popup_linkedin)
        await self.context.route(
            "**careers.example.test/**",
            lambda route: route.fulfill(content_type="text/html", body="<form></form>"),
        )
        linkedin = await self.context.new_page()
        await linkedin.goto(LINKEDIN_URL)

        session = await ExternalApplicationLauncher.open(
            linkedin, linkedin.locator("button.jobs-apply-button")
        )

        self.assertEqual(linkedin.url, LINKEDIN_URL)
        self.assertEqual(session.application_page.url, ATS_URL)
        await session.close()

    async def test_direct_link_follows_multiple_redirects(self):
        first = "https://redirect.example.test/start"
        second = "https://redirect.example.test/continue"

        async def linked_route(route):
            await route.fulfill(
                content_type="text/html",
                body=f"<a class='jobs-apply-button' href='{first}'>Apply</a>",
            )

        await self.context.unroute("**/*")
        async def redirects(route):
            url = route.request.url
            if "linkedin.com" in url:
                await linked_route(route)
            elif url == first:
                await route.fulfill(
                    content_type="text/html",
                    body=f"<script>location.replace('{second}')</script>",
                )
            elif url == second:
                await route.fulfill(
                    content_type="text/html",
                    body=f"<script>location.replace('{ATS_URL}')</script>",
                )
            elif url == ATS_URL:
                await route.fulfill(content_type="text/html", body="<form></form>")
            else:
                await route.abort()

        await self.context.route("**/*", redirects)
        linkedin = await self.context.new_page()
        await linkedin.goto(LINKEDIN_URL)

        session = await ExternalApplicationLauncher.open(
            linkedin, linkedin.locator("a.jobs-apply-button")
        )

        self.assertEqual(linkedin.url, LINKEDIN_URL)
        self.assertEqual(session.application_page.url, ATS_URL)
        await session.close()

    def test_canonical_url_removes_tracking_and_fragment(self):
        self.assertEqual(
            ExternalApplicationLauncher.canonical_url(
                "https://Jobs.Lever.co/acme/123/?utm_source=linkedin&ref=x#apply"
            ),
            "https://jobs.lever.co/acme/123",
        )


if __name__ == "__main__":
    unittest.main()
