import unittest

from src.review import ReviewCoordinator


class ReviewCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_decision_resumes_waiting_browser_task(self):
        coordinator = ReviewCoordinator()
        waiter = coordinator.create_waiter(42)

        self.assertTrue(coordinator.is_active(42))
        self.assertTrue(coordinator.decide(42, "approved"))
        self.assertEqual(await waiter, "approved")
        self.assertFalse(coordinator.decide(42, "rejected"))

        coordinator.finish(42)
        self.assertFalse(coordinator.is_active(42))

    async def test_invalid_decision_is_rejected(self):
        coordinator = ReviewCoordinator()
        coordinator.create_waiter(7)

        with self.assertRaises(ValueError):
            coordinator.decide(7, "maybe")


if __name__ == "__main__":
    unittest.main()
