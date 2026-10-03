"""LinkedIn job-page metadata extraction with layered selector fallbacks."""

import asyncio
import json
import re
import html
from urllib.parse import urljoin, urlsplit, parse_qs
from typing import Any, Dict, Iterable, List, Optional

from playwright.async_api import Locator, Page

from src.models import JobRecord
from src.ats.registry import AdapterRegistry


class JobExtractionError(ValueError):
    """A listing could not be read well enough to evaluate its match."""


class LinkedInJobExtractor:
    """Enrich a discovered JobRecord from the currently open LinkedIn page."""

    WORKPLACE_TYPES = ("Remote", "Hybrid", "On-site")
    EMPLOYMENT_TYPES = (
        "Full-time", "Part-time", "Contract", "Temporary", "Internship", "Volunteer"
    )

    def __init__(self, page: Page):
        self.page = page

    @staticmethod
    def has_meaningful_details(record: JobRecord) -> bool:
        title = record.title.strip().lower()
        description = record.description.strip()
        return (bool(title) and title not in {"jobs", "linkedin", "loading", "loading...", "about the job"}
                and len(description) >= 80 and len(description.split()) >= 12)

    async def extract_for_search(self, record: JobRecord, timeout: float = 20) -> JobRecord:
        """Wait for a readable listing without interacting with application controls.

        The current LinkedIn layout has paragraph titles and hashed classes.
        Read its semantic job section as well as legacy/JSON-LD fallbacks. Full
        text already present in collapsed descriptions is read without clicking.
        Autopilot retains its existing extract() path.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        previous = None
        while True:
            await self.extract(record)
            details = await self._search_dom_details()
            for field in ("title", "company", "location"):
                if details[field]:
                    setattr(record, field, self._clean_value(details[field]))
            if len(details["description"]) > len(record.description):
                record.description = details["description"]
            current = (record.title, record.company, record.location, record.description)
            if self.has_meaningful_details(record) and current == previous and not details["busy"]:
                return record
            if asyncio.get_running_loop().time() >= deadline:
                raise JobExtractionError("Title or meaningful full description unavailable, or job content did not finish loading")
            previous = current
            await asyncio.sleep(0.4)

    async def _search_dom_details(self) -> Dict[str, Any]:
        return await self.page.evaluate(r"""() => {
            const main = document.querySelector('main') || document.body;
            const visible = el => !!el && !!(el.getClientRects().length);
            const text = el => (el?.innerText || '').trim();
            const headings = [...main.querySelectorAll('h2,h3,[role="heading"]')];
            const about = headings.find(el => visible(el) && /^About the job$/i.test(text(el)));
            let descriptionBox = null;
            if (about) {
                // Select the nearest job-description section, never the company
                // description or recommended jobs further down the page.
                for (let parent = about.parentElement; parent && parent !== main; parent = parent.parentElement) {
                    if ([...parent.querySelectorAll('h2,h3,[role="heading"]')].some(h => h !== about)) break;
                    descriptionBox = parent.querySelector('[data-testid="expandable-text-box"], [itemprop="description"]');
                    if (descriptionBox) break;
                }
            }
            const descriptions = [descriptionBox, ...main.querySelectorAll(
                '#job-details, .jobs-description__content, .jobs-box__html-content, '
                + '.show-more-less-html__markup, [itemprop="description"]'
            )].filter(visible);
            const fullText = el => {
                const walk = node => {
                    if (node.nodeType === Node.TEXT_NODE) return node.textContent;
                    if (node.nodeType !== Node.ELEMENT_NODE || node.matches('button,script,style,svg')) return '';
                    const value = [...node.childNodes].map(walk).join('');
                    return /^(P|LI|DIV|BR|H[1-6])$/.test(node.tagName) ? '\n' + value + '\n' : value;
                };
                return walk(el).replace(/[ \t]+/g, ' ').replace(/\n\s*\n/g, '\n\n').trim();
            };
            const description = descriptions.map(fullText).sort((a,b) => b.length - a.length)[0] || '';
            // Live LinkedIn's company accessibility label anchors the top card.
            const companyNode = [...main.querySelectorAll('[aria-label^="Company,"]')]
                .find(el => visible(el) && (!about || (el.compareDocumentPosition(about) & Node.DOCUMENT_POSITION_FOLLOWING)));
            let title = '', company = '', location = '';
            if (companyNode) {
                company = text(companyNode.querySelector('a[href*="/company/"]'))
                    || companyNode.getAttribute('aria-label').replace(/^Company,\s*/, '').replace(/\.$/, '');
                for (let parent = companyNode.parentElement; parent && parent !== main; parent = parent.parentElement) {
                    const paragraphs = [...parent.querySelectorAll('p')].filter(el => visible(el)
                        && !companyNode.contains(el) && !el.contains(companyNode)
                        && (companyNode.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)
                        && (!about || (el.compareDocumentPosition(about) & Node.DOCUMENT_POSITION_FOLLOWING)));
                    if (paragraphs.length >= 2) {
                        title = text(paragraphs[0]);
                        location = text(paragraphs[1]).split('·')[0].trim();
                        break;
                    }
                }
            }
            return {title, company, location, description,
                busy: [...main.querySelectorAll('[aria-busy="true"]')].some(visible)};
        }""")

    async def extract(self, record: JobRecord) -> JobRecord:
        """Update and return the same record, retaining discovery fallbacks."""
        structured = await self._job_posting_json()
        title = await self._first_title(
            self.page.locator(
                "h1.job-details-jobs-unified-top-card__job-title, "
                ".job-details-jobs-unified-top-card__job-title h1, "
                ".job-details-jobs-unified-top-card__job-title, "
                "h1.top-card-layout__title"
            ),
            self.page.get_by_role("heading", level=1),
        )
        company = await self._first_text(
            self.page.locator(
                ".job-details-jobs-unified-top-card__company-name a, "
                ".job-details-jobs-unified-top-card__company-name, "
                "a.topcard__org-name-link, .topcard__flavor-row a"
            )
        )
        location_candidates = await self._visible_texts(
            self.page.locator(
                ".job-details-jobs-unified-top-card__primary-description-container "
                ".tvm__text--low-emphasis, "
                ".job-details-jobs-unified-top-card__primary-description-container span, "
                ".topcard__flavor--bullet, .topcard__flavor--metadata"
            )
        )
        description = await self._first_text(
            self.page.locator(
                ".jobs-description__content, .jobs-box__html-content, "
                "article.jobs-description, #job-details"
            )
        )
        page_text = await self._main_text()
        insight_text = " ".join(
            await self._visible_texts(
                self.page.locator(
                    ".job-details-jobs-unified-top-card__job-insight, "
                    ".job-details-preferences-and-skills, "
                    ".description__job-criteria-list"
                )
            )
        )
        metadata_text = insight_text or page_text[:15000]

        structured_company = structured.get("hiringOrganization", {})
        if not isinstance(structured_company, dict):
            structured_company = {}
        record.title = (
            self.clean_title(title)
            or self.clean_title(str(structured.get("title") or ""))
            or self.clean_title(record.title)
        )
        record.company = (
            self._clean_value(company)
            or self._clean_value(str(structured_company.get("name") or ""))
            or record.company
        )
        record.location = self._choose_location(
            location_candidates, excluded=(title, company)
        ) or self._structured_location(structured) or record.location
        structured_description = html.unescape(re.sub(r"<[^>]+>", " ", str(structured.get("description") or "")))
        record.description = self._clean_value(description) or self._clean_value(structured_description) or record.description
        record.external_job_id = record.external_job_id or self.job_id_from_url(record.job_url)
        record.source = record.source or "LinkedIn"
        record.workplace_type = self._detect_label(metadata_text, self.WORKPLACE_TYPES)
        record.employment_type = self._detect_label(metadata_text, self.EMPLOYMENT_TYPES)
        if not record.employment_type:
            record.employment_type = self._structured_employment_type(
                structured.get("employmentType")
            )
        record.salary = self._extract_salary(f"{insight_text} {page_text[:30000]}")
        record.posted_date = await self._posted_date() or self._clean_value(
            str(structured.get("datePosted") or "")
        )

        easy_apply = await self._first_visible(
            self.page.locator(
                "button[aria-label*='Easy Apply' i], a[aria-label*='Easy Apply' i], "
                "button:has-text('Easy Apply'), a:has-text('Easy Apply')"
            )
        )
        any_apply = await self._first_visible(
            self.page.locator(
                "button.jobs-apply-button, a.jobs-apply-button, button[aria-label*='Apply' i], "
                "a[aria-label*='Apply' i], main a:has-text('Apply'), main button:has-text('Apply')"
            )
        )
        record.easy_apply = easy_apply is not None
        record.external_apply = easy_apply is None and any_apply is not None
        record.application_method = ("easy_apply" if record.easy_apply else
                                     "external" if record.external_apply else "unavailable")
        if record.external_apply:
            # Read only: Search & Save must never click Apply or resolve redirects.
            for attribute in ("href", "data-apply-url", "data-external-url"):
                raw = await any_apply.get_attribute(attribute)
                if not raw or raw.startswith("#"):
                    continue
                url = urljoin(self.page.url, raw)
                parsed = urlsplit(url)
                if parsed.hostname and (parsed.hostname == "linkedin.com" or parsed.hostname.endswith(".linkedin.com")):
                    destination = parse_qs(parsed.query).get("url", [""])[0]
                    if destination:
                        url = destination
                        parsed = urlsplit(url)
                if parsed.scheme in {"http", "https"} and url != record.job_url:
                    record.application_url = url
                    record.ats_type = AdapterRegistry.provider_from_url(url) or record.ats_type
                    break
        if record.easy_apply:
            record.ats_type = record.ats_type or "LinkedIn Easy Apply"
        return record

    async def _job_posting_json(self) -> Dict[str, Any]:
        scripts = self.page.locator("script[type='application/ld+json']")
        for index in range(min(await scripts.count(), 10)):
            try:
                raw = await scripts.nth(index).text_content() or ""
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            posting = self._find_job_posting(parsed)
            if posting:
                return posting
        return {}

    @classmethod
    def _find_job_posting(cls, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            item_type = value.get("@type", "")
            types = item_type if isinstance(item_type, list) else [item_type]
            if "JobPosting" in types:
                return value
            for child in value.values():
                found = cls._find_job_posting(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._find_job_posting(child)
                if found:
                    return found
        return {}

    @classmethod
    def _structured_location(cls, posting: Dict[str, Any]) -> str:
        locations = posting.get("jobLocation", [])
        if isinstance(locations, dict):
            locations = [locations]
        for location in locations if isinstance(locations, list) else []:
            if not isinstance(location, dict):
                continue
            address = location.get("address", location)
            if not isinstance(address, dict):
                continue
            parts = [
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("addressCountry"),
            ]
            cleaned = [cls._clean_value(str(part)) for part in parts if part]
            if cleaned:
                return ", ".join(cleaned)
        return ""

    @classmethod
    def _structured_employment_type(cls, value: Any) -> str:
        if isinstance(value, list):
            value = value[0] if value else ""
        normalized = cls._clean_value(str(value)).replace("_", " ").lower()
        mapping = {
            "full time": "Full-time",
            "part time": "Part-time",
            "contractor": "Contract",
            "contract": "Contract",
            "temporary": "Temporary",
            "intern": "Internship",
            "internship": "Internship",
            "volunteer": "Volunteer",
        }
        return mapping.get(normalized, "")

    async def _posted_date(self) -> str:
        time_element = await self._first_visible(self.page.locator("time[datetime]"))
        if time_element is not None:
            value = (await time_element.get_attribute("datetime") or "").strip()
            if value:
                return value
            text = (await time_element.inner_text()).strip()
            if text:
                return text
        candidates = await self._visible_texts(
            self.page.locator(
                ".job-details-jobs-unified-top-card__primary-description-container span, "
                ".posted-time-ago__text"
            )
        )
        return next(
            (
                value
                for value in candidates
                if re.search(r"\b(?:minute|hour|day|week|month)s? ago\b|\breposted\b", value, re.I)
            ),
            "",
        )

    async def _main_text(self) -> str:
        main = await self._first_visible(self.page.locator("main"))
        if main is None:
            main = await self._first_visible(self.page.locator("body"))
        if main is None:
            return ""
        try:
            return (await main.inner_text(timeout=5_000))[:80_000]
        except Exception:
            return ""

    @staticmethod
    def job_id_from_url(url: str) -> str:
        match = re.search(r"/jobs/view/(?:[^/?#]+-)?(\d+)", url, re.I)
        return match.group(1) if match else ""

    @classmethod
    def _choose_location(
        cls, candidates: Iterable[str], excluded: Iterable[str] = ()
    ) -> str:
        excluded_values = {cls._clean_value(value).lower() for value in excluded if value}
        for candidate in candidates:
            value = cls._clean_value(candidate).strip(" ·")
            if not value or len(value) > 120 or value.lower() in excluded_values:
                continue
            if re.search(
                r"\b(?:applicant|minute|hour|day|week|month)s?\b|\breposted\b|\bago\b",
                value,
                re.I,
            ):
                continue
            if value.lower() in {"remote", "hybrid", "on-site", "onsite"}:
                continue
            return value
        return ""

    @staticmethod
    def _detect_label(text: str, labels: Iterable[str]) -> str:
        normalized = re.sub(r"[^a-z0-9]+", " ", text.lower())
        for label in labels:
            token = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
            if re.search(rf"\b{re.escape(token)}\b", normalized):
                return label
        return ""

    @staticmethod
    def _extract_salary(text: str) -> str:
        patterns = (
            r"\$\s?\d{2,3}(?:,\d{3})?(?:\.\d+)?\s*(?:-|–|—|to)\s*"
            r"\$?\s?\d{2,3}(?:,\d{3})?(?:\.\d+)?(?:\s*(?:a year|per year|/yr|/hour|/hr))?",
            r"\$\s?\d{2,3}(?:,\d{3})?(?:\.\d+)?\+?\s*(?:a year|per year|/yr|/hour|/hr)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                return " ".join(match.group(0).split())
        return ""

    @staticmethod
    def _clean_value(value: str) -> str:
        return " ".join((value or "").split()).strip()

    @classmethod
    def clean_title(cls, value: str) -> str:
        # Only the exact trailing LinkedIn badge phrase, not words such as
        # "Verification" or "Verified" that can belong to a real job title.
        return re.sub(r"\s+with verification$", "", cls._clean_value(value), flags=re.I)

    @classmethod
    async def _first_title(cls, *locators: Locator) -> str:
        for locator in locators:
            candidate = await cls._first_visible(locator)
            if candidate is not None:
                text = await candidate.evaluate("""element => {
                    const copy = element.cloneNode(true);
                    copy.querySelectorAll('svg, [role="img"], [data-test-icon], .visually-hidden, .sr-only')
                        .forEach(node => {
                            const description = [node.textContent, node.getAttribute('aria-label'),
                                node.getAttribute('data-test-icon')].join(' ');
                            if (/verif(?:ied|ication)|verified-badge/i.test(description)) node.remove();
                        });
                    return copy.textContent;
                }""")
                if cls.clean_title(text):
                    return cls.clean_title(text)
        return ""

    @staticmethod
    async def _first_visible(locator: Locator) -> Optional[Locator]:
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
        return None

    @classmethod
    async def _first_text(cls, *locators: Locator) -> str:
        for locator in locators:
            candidate = await cls._first_visible(locator)
            if candidate is not None:
                text = cls._clean_value(await candidate.inner_text())
                if text:
                    return text
        return ""

    @classmethod
    async def _visible_texts(cls, locator: Locator) -> List[str]:
        result: List[str] = []
        for index in range(min(await locator.count(), 30)):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                text = cls._clean_value(await candidate.inner_text())
                if text and text not in result:
                    result.append(text)
        return result
