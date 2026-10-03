"""Offline dashboard coverage for combined filters and scoped deletion."""
import unittest

import tests.test_search_only as fixtures
from src.models import JobRecord


class DashboardControlsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.SearchOnlyTests.asyncSetUp
    stop_server = fixtures.SearchOnlyTests.stop_server
    fixture = fixtures.SearchOnlyTests.fixture

    def save(self, index, status="SAVED_FOR_REVIEW", source="search_save", method="external"):
        url = f"https://www.linkedin.com/jobs/view/{910000 + index}/"
        self.db.save_job_record(JobRecord(job_url=url, title="Analyst", status=status,
                                         source_mode=source, application_method=method))
        return url

    async def test_method_filters_intersect_source_in_every_tab(self):
        views = [("reviews", "review", "review-container", "SAVED_FOR_REVIEW"),
                 ("applied", "applied", "applied-container", "APPLIED"),
                 ("rejected", "rejected", "rejected-container", "REJECTED"),
                 ("search-issues", "skipped", "search-issues-container", "SKIPPED"),
                 ("flagged", "failed", "flagged-container", "FAILED")]
        for index, (_, _, _, status) in enumerate(views):
            for offset, (source, method) in enumerate((('search_save', 'external'), ('search_save', 'easy_apply'),
                                                     ('autopilot', 'external'), ('autopilot', 'easy_apply'))):
                self.save(index * 4 + offset, status, source, method)
        await self.ui.reload()
        for tab, bucket, container, _ in views:
            await self.ui.locator(f'[data-tab="tab-{tab}"]').click()
            await self.ui.locator(f'#{container} .review-card').first.wait_for()
            self.assertEqual(await self.ui.locator(f'#{container} .review-card').count(), 4)
            await self.ui.locator(f'#method-filters-{bucket}').get_by_role('button', name='External (2)', exact=True).click()
            self.assertEqual(await self.ui.locator(f'#{container} .review-card').count(), 2)
            await self.ui.locator(f'#source-filters-{bucket}').get_by_role('button', name='Search & Save (1)', exact=True).click()
            self.assertEqual(await self.ui.locator(f'#{container} .review-card').count(), 1)
            self.assertEqual(await self.ui.locator(f'#{container} .source-badge').inner_text(), 'SEARCH')
            self.assertEqual(await self.ui.locator(f'#{container} .application-method-badge').inner_text(), 'EXTERNAL')
            await self.ui.locator(f'#method-filters-{bucket}').get_by_role('button', name='Easy Apply (1)', exact=True).click()
            self.assertEqual(await self.ui.locator(f'#{container} .application-method-badge').inner_text(), 'EASY APPLY')
            await self.ui.locator(f'#method-filters-{bucket}').get_by_role('button', name='All (2)', exact=True).click()
            self.assertEqual(await self.ui.locator(f'#{container} .review-card').count(), 2)
        self.assertEqual(await self.ui.locator('#delete-controls-applied').count(), 0)
        self.assertEqual(await self.ui.locator('#applied-container .job-delete-controls').count(), 0)
        self.assertEqual(self.errors, [])

    async def test_clear_single_and_selected_delete_preserve_hidden_history(self):
        urls = [self.save(0), self.save(1), self.save(2, method='easy_apply'),
                self.save(3, source='autopilot'), self.save(4, source='autopilot'),
                self.save(5, 'APPLIED'), self.save(6, 'REJECTED'), self.save(7, 'FAILED')]
        history = self.db.get_applied_jobs()
        await self.ui.reload()
        await self.ui.locator('[data-tab="tab-reviews"]').click()
        await self.ui.locator('#source-filters-review').get_by_role('button', name='Search & Save (3)', exact=True).click()
        await self.ui.locator('#method-filters-review').get_by_role('button', name='External (2)', exact=True).click()
        cancelled = []
        async def cancel(dialog):
            cancelled.append(dialog.message)
            await dialog.dismiss()
        self.ui.once('dialog', cancel)
        await self.ui.get_by_role('button', name='Clear current filtered list (2)', exact=True).click()
        self.assertEqual(cancelled, ['Delete 2 filtered jobs? This cannot be undone.'])
        self.assertEqual(len(self.db.get_dashboard_jobs()), 8)
        messages = []
        async def confirm(dialog):
            messages.append(dialog.message)
            await dialog.accept()
        self.ui.on('dialog', confirm)
        await self.ui.get_by_role('button', name='Clear current filtered list (2)', exact=True).click()
        await self.ui.wait_for_function("document.querySelector('#badge-reviews').textContent === '3'")
        self.assertEqual({row['job_url'] for row in self.db.get_dashboard_jobs()}, set(urls[2:]))
        await self.ui.locator('#method-filters-review').get_by_role('button', name='All (1)', exact=True).click()
        await self.ui.locator('#review-container').get_by_role('button', name='Delete job', exact=True).click()
        await self.ui.wait_for_function("document.querySelector('#badge-reviews').textContent === '2'")
        await self.ui.locator('#source-filters-review').get_by_role('button', name='Autopilot (2)', exact=True).click()
        await self.ui.locator('#review-container input[aria-label="Select job for deletion"]').nth(0).check()
        await self.ui.locator('#review-container input[aria-label="Select job for deletion"]').nth(1).check()
        await self.ui.get_by_role('button', name='Delete selected (2)', exact=True).click()
        await self.ui.wait_for_function("document.querySelector('#badge-reviews').textContent === '0'")
        self.assertEqual({row['job_url'] for row in self.db.get_dashboard_jobs()}, set(urls[5:]))
        self.assertEqual(self.db.get_applied_jobs(), history)
        self.assertEqual(messages, ['Delete 2 filtered jobs? This cannot be undone.',
                                    'Delete 1 job? This cannot be undone.', 'Delete 2 jobs? This cannot be undone.'])
        await self.ui.reload()
        self.assertEqual(len(self.db.get_dashboard_jobs()), 3)
        response = await self.ui.request.post(self.base + '/api/jobs/delete', data={'job_urls': [urls[5]], 'bucket': 'review'})
        self.assertEqual(response.status, 409)
        self.assertEqual(self.db.get_applied_jobs(), history)
        self.assertEqual(self.errors, [])

    async def test_applied_history_stays_protected_after_immediate_status_moves(self):
        url = self.save(30)
        await self.ui.reload()
        await self.ui.locator('[data-tab="tab-reviews"]').click()
        await self.ui.locator('#review-container select[data-job-url]').select_option('Applied')
        await self.ui.locator('[data-tab="tab-applied"]').click()
        await self.ui.locator('#applied-container select[data-job-url]').select_option('Pending')
        await self.ui.locator('[data-tab="tab-reviews"]').click()
        await self.ui.locator('#review-container select[data-job-url]').wait_for()
        self.assertEqual(await self.ui.locator('#review-container .job-delete-controls').count(), 0)
        self.assertTrue(await self.ui.get_by_role('button', name='Clear current filtered list (0)', exact=True).is_disabled())
        response = await self.ui.request.post(self.base + '/api/jobs/delete', data={'job_urls': [url], 'bucket': 'review'})
        self.assertEqual(response.status, 409)
        self.assertEqual(len(self.db.get_dashboard_jobs()), 1)
        self.assertEqual(self.errors, [])
