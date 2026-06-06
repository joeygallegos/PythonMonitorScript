import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace


def install_import_stubs():
    if "requests" not in sys.modules:
        sys.modules["requests"] = ModuleType("requests")

    if "aiohttp" not in sys.modules:
        aiohttp = ModuleType("aiohttp")
        aiohttp.ClientTimeout = lambda *args, **kwargs: None
        aiohttp.ClientSession = lambda *args, **kwargs: None
        sys.modules["aiohttp"] = aiohttp

    if "multidict" not in sys.modules:
        multidict = ModuleType("multidict")
        multidict.CIMultiDictProxy = type("CIMultiDictProxy", (), {})
        sys.modules["multidict"] = multidict

    if "selenium" not in sys.modules:
        selenium = ModuleType("selenium")
        webdriver = ModuleType("webdriver")
        webdriver.ChromeOptions = lambda: SimpleNamespace(add_argument=lambda *_: None)
        webdriver.Chrome = lambda *args, **kwargs: None
        selenium.webdriver = webdriver
        sys.modules["selenium"] = selenium
        sys.modules["selenium.webdriver"] = webdriver


install_import_stubs()
import run  # noqa: E402


class CertificateMonitoringTests(unittest.TestCase):
    def test_should_run_certificate_check_once_per_utc_day(self):
        self.assertFalse(
            run.should_run_certificate_check(
                {"certificate_last_checked_date": "2026-06-06"}, "2026-06-06"
            )
        )
        self.assertTrue(
            run.should_run_certificate_check(
                {"certificate_last_checked_date": "2026-06-05"}, "2026-06-06"
            )
        )
        self.assertTrue(run.should_run_certificate_check({}, "2026-06-06"))

    def test_certificate_alert_threshold_is_ten_days_or_less(self):
        now = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)

        self.assertFalse(run.certificate_needs_alert(now + timedelta(days=11), now))
        self.assertTrue(run.certificate_needs_alert(now + timedelta(days=10), now))
        self.assertTrue(run.certificate_needs_alert(now + timedelta(days=1), now))
        self.assertTrue(run.certificate_needs_alert(now, now))
        self.assertTrue(run.certificate_needs_alert(now - timedelta(days=1), now))

    def test_certificate_days_remaining_rounds_up_partial_days(self):
        now = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(11, run.certificate_days_remaining(now + timedelta(days=10, seconds=1), now))
        self.assertEqual(10, run.certificate_days_remaining(now + timedelta(days=10), now))
        self.assertEqual(1, run.certificate_days_remaining(now + timedelta(seconds=1), now))
        self.assertEqual(0, run.certificate_days_remaining(now, now))
        self.assertEqual(0, run.certificate_days_remaining(now - timedelta(seconds=1), now))
        self.assertEqual(-1, run.certificate_days_remaining(now - timedelta(days=1), now))

    def test_update_certificate_tracking_preserves_incident_state(self):
        original_state = {
            "incident_active": True,
            "incident_start": "2026-06-06T10:00:00+00:00",
            "incident_last_seen": "2026-06-06T10:05:00+00:00",
            "incident_duration": "5m 0s",
            "failures_total": 3,
        }
        saved_state = {}

        self.patch_tracking_io(original_state, saved_state)

        updated = run.update_certificate_tracking("2026-06-06", alerted=True)

        self.assertTrue(updated["incident_active"])
        self.assertEqual("2026-06-06T10:00:00+00:00", updated["incident_start"])
        self.assertEqual(3, updated["failures_total"])
        self.assertEqual("2026-06-06", updated["certificate_last_checked_date"])
        self.assertEqual("2026-06-06", updated["certificate_last_alerted_date"])
        self.assertEqual(updated, saved_state)

    def patch_tracking_io(self, original_state, saved_state):
        self.addCleanup(setattr, run, "load_tracking", run.load_tracking)
        self.addCleanup(setattr, run, "save_tracking", run.save_tracking)
        run.load_tracking = lambda: copy.deepcopy(original_state)
        run.save_tracking = lambda data: saved_state.update(copy.deepcopy(data))


class IncidentTrackingTests(unittest.TestCase):
    def setUp(self):
        self.original_print = run.print if hasattr(run, "print") else print
        run.print = lambda *args, **kwargs: None
        self.addCleanup(setattr, run, "print", self.original_print)

    def test_update_incident_tracking_starts_new_incident(self):
        original_state = {
            "incident_active": False,
            "incident_start": None,
            "incident_last_seen": None,
            "incident_duration": "0s",
            "failures_total": 0,
            "certificate_last_checked_date": "2026-06-06",
        }
        saved_state = {}
        self.patch_tracking_io(original_state, saved_state)

        updated = run.update_incident_tracking(2)

        self.assertTrue(updated["incident_active"])
        self.assertEqual(2, updated["failures_total"])
        self.assertIsNotNone(updated["incident_start"])
        self.assertIsNotNone(updated["incident_last_seen"])
        self.assertEqual("2026-06-06", updated["certificate_last_checked_date"])
        self.assertEqual(updated, saved_state)

    def test_update_incident_tracking_increments_existing_incident(self):
        original_state = {
            "incident_active": True,
            "incident_start": "2026-06-06T10:00:00+00:00",
            "incident_last_seen": "2026-06-06T10:05:00+00:00",
            "incident_duration": "5m 0s",
            "failures_total": 3,
            "certificate_last_checked_date": "2026-06-06",
        }
        saved_state = {}
        self.patch_tracking_io(original_state, saved_state)

        updated = run.update_incident_tracking(2)

        self.assertTrue(updated["incident_active"])
        self.assertEqual(5, updated["failures_total"])
        self.assertEqual("2026-06-06T10:00:00+00:00", updated["incident_start"])
        self.assertEqual("2026-06-06", updated["certificate_last_checked_date"])
        self.assertEqual(updated, saved_state)

    def test_update_incident_tracking_resets_incident_but_preserves_certificate_state(self):
        original_state = {
            "incident_active": True,
            "incident_start": "2026-06-06T10:00:00+00:00",
            "incident_last_seen": "2026-06-06T10:05:00+00:00",
            "incident_duration": "5m 0s",
            "failures_total": 3,
            "certificate_last_checked_date": "2026-06-06",
            "certificate_last_alerted_date": "2026-06-06",
        }
        saved_state = {}
        self.patch_tracking_io(original_state, saved_state)

        updated = run.update_incident_tracking(0)

        self.assertFalse(updated["incident_active"])
        self.assertIsNone(updated["incident_start"])
        self.assertIsNone(updated["incident_last_seen"])
        self.assertEqual("0s", updated["incident_duration"])
        self.assertEqual(0, updated["failures_total"])
        self.assertEqual("2026-06-06", updated["certificate_last_checked_date"])
        self.assertEqual("2026-06-06", updated["certificate_last_alerted_date"])
        self.assertEqual(updated, saved_state)

    def patch_tracking_io(self, original_state, saved_state):
        self.addCleanup(setattr, run, "load_tracking", run.load_tracking)
        self.addCleanup(setattr, run, "save_tracking", run.save_tracking)
        run.load_tracking = lambda: copy.deepcopy(original_state)
        run.save_tracking = lambda data: saved_state.update(copy.deepcopy(data))


class EmailMarkupTests(unittest.TestCase):
    def setUp(self):
        self.original_screenshots_enabled = getattr(run, "SCREENSHOTS_ENABLED", False)
        run.SCREENSHOTS_ENABLED = False
        self.addCleanup(setattr, run, "SCREENSHOTS_ENABLED", self.original_screenshots_enabled)

    def test_certificate_email_markup_includes_expiry_details(self):
        markup = run.get_certificate_email_markup(
            [
                {
                    "domain": "example.com",
                    "expires_at": datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc),
                    "days_remaining": 10,
                    "exception": None,
                }
            ]
        )

        self.assertIn("Certificate warning for example.com", markup)
        self.assertIn("Days remaining:</strong> 10", markup)
        self.assertIn("Jun 16, 2026 12:00 PM UTC", markup)

    def test_certificate_email_markup_escapes_domain_and_exception(self):
        markup = run.get_certificate_email_markup(
            [
                {
                    "domain": "<example.com>",
                    "expires_at": None,
                    "days_remaining": None,
                    "exception": "<bad cert>",
                }
            ]
        )

        self.assertIn("&lt;example.com&gt;", markup)
        self.assertIn("&lt;bad cert&gt;", markup)
        self.assertNotIn("<example.com>", markup)
        self.assertNotIn("<bad cert>", markup)


if __name__ == "__main__":
    unittest.main()
