import copy
import configparser
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


class SiteSchemeTests(unittest.TestCase):
    def setUp(self):
        self.original_print = run.print if hasattr(run, "print") else print
        run.print = lambda *args, **kwargs: None
        self.addCleanup(setattr, run, "print", self.original_print)

    def test_site_scheme_defaults_to_https(self):
        sites = {
            "sites": {
                "example.com": {
                    "check": True,
                    "endpoints": {"/": {"status": 200, "dom_contains": ""}},
                }
            }
        }

        self.assertEqual("https", run.get_site_scheme(sites["sites"]["example.com"]))
        self.assertEqual("https://example.com/", run.build_endpoint_url(sites, "example.com", "/"))

    def test_site_scheme_supports_http(self):
        sites = {
            "sites": {
                "localhost:8000": {
                    "check": True,
                    "scheme": "http",
                    "endpoints": {"/": {"status": 200, "dom_contains": ""}},
                }
            }
        }

        self.assertEqual("http", run.get_site_scheme(sites["sites"]["localhost:8000"]))
        self.assertEqual("http://localhost:8000/", run.build_endpoint_url(sites, "localhost:8000", "/"))

    def test_site_scheme_rejects_unknown_values(self):
        with self.assertRaises(ValueError):
            run.get_site_scheme({"scheme": "ftp"})

    def test_certificate_checks_skip_http_sites(self):
        sites = {
            "sites": {
                "localhost:8000": {
                    "check": True,
                    "scheme": "http",
                    "endpoints": {"/": {"status": 200, "dom_contains": ""}},
                }
            }
        }
        original_get_certificate_expiry = run.get_certificate_expiry
        run.get_certificate_expiry = lambda *_args, **_kwargs: self.fail(
            "HTTP sites should not fetch HTTPS certificates"
        )
        self.addCleanup(setattr, run, "get_certificate_expiry", original_get_certificate_expiry)

        self.assertEqual([], run.do_certificate_checks(sites))


class DnsBaselineTests(unittest.TestCase):
    def setUp(self):
        self.original_print = run.print if hasattr(run, "print") else print
        run.print = lambda *args, **kwargs: None
        self.addCleanup(setattr, run, "print", self.original_print)

    def make_sites(self, records=None, check=True):
        site_config = {
            "check": check,
            "endpoints": {"/": {"status": 200, "dom_contains": ""}},
        }
        if records is not None:
            site_config["dns_records"] = records

        return {
            "sites": {
                "example.com": site_config
            }
        }

    def test_parse_args_supports_dns_subcommands(self):
        self.assertEqual("monitor", run.parse_args([]).command)
        self.assertEqual("dns-scan", run.parse_args(["dns-scan"]).command)
        self.assertEqual("dns-baseline", run.parse_args(["dns-baseline"]).command)

    def test_dns_normalization_supports_common_record_types(self):
        mx_value = SimpleNamespace(preference=10, exchange="Mail.Example.COM")
        txt_value = SimpleNamespace(strings=[b"v=spf1 ", b"include:_spf.example.com"])

        self.assertEqual(
            "example.com.", run.normalize_dns_record_value("CNAME", "Example.COM")
        )
        self.assertEqual("ns1.example.com.", run.normalize_dns_record_value("NS", "NS1.Example.COM."))
        self.assertEqual("192.0.2.1", run.normalize_dns_record_value("A", "192.0.2.1"))
        self.assertEqual("2001:db8::1", run.normalize_dns_record_value("AAAA", "2001:db8::1"))
        self.assertEqual("10 mail.example.com.", run.normalize_dns_record_value("MX", mx_value))
        self.assertEqual(
            "v=spf1 include:_spf.example.com",
            run.normalize_dns_record_value("TXT", txt_value),
        )

    def test_build_dns_scan_document_resolves_enabled_configured_records(self):
        def resolver(name, record_type, lifetime=5):
            self.assertEqual("example.com", name)
            self.assertEqual("A", record_type)
            self.assertEqual(5, lifetime)
            return ["192.0.2.1"]

        scan = run.build_dns_scan_document(
            self.make_sites([{"name": "example.com", "type": "A"}]),
            resolver,
            "2026-06-06T12:00:00+00:00",
        )

        self.assertEqual("2026-06-06T12:00:00+00:00", scan["generated_at"])
        self.assertEqual(
            [
                {
                    "site": "example.com",
                    "name": "example.com",
                    "type": "A",
                    "values": ["192.0.2.1"],
                    "error": None,
                }
            ],
            scan["records"],
        )

    def test_configured_dns_records_inherit_from_parent_site_domain(self):
        records = run.get_configured_dns_records(self.make_sites())

        self.assertEqual(
            [
                {"site": "example.com", "name": "example.com", "type": "A"},
                {"site": "example.com", "name": "example.com", "type": "AAAA"},
                {"site": "example.com", "name": "example.com", "type": "MX"},
                {"site": "example.com", "name": "example.com", "type": "TXT"},
                {"site": "example.com", "name": "example.com", "type": "NS"},
            ],
            records,
        )

    def test_configured_dns_record_name_defaults_to_parent_site_domain(self):
        records = run.get_configured_dns_records(
            self.make_sites([{"type": "TXT"}])
        )

        self.assertEqual(
            [{"site": "example.com", "name": "example.com", "type": "TXT"}],
            records,
        )

    def test_build_dns_scan_document_skips_disabled_sites(self):
        scan = run.build_dns_scan_document(
            self.make_sites(check=False), lambda *_args, **_kwargs: self.fail()
        )

        self.assertEqual([], scan["records"])

    def test_build_dns_scan_document_treats_no_answer_as_empty_values(self):
        class FakeNoAnswer(Exception):
            pass

        original_dns_resolver = run.dns_resolver
        run.dns_resolver = SimpleNamespace(NoAnswer=FakeNoAnswer)
        self.addCleanup(setattr, run, "dns_resolver", original_dns_resolver)

        def resolver(_name, _record_type, lifetime=5):
            raise FakeNoAnswer("no AAAA")

        scan = run.build_dns_scan_document(
            self.make_sites([{"name": "example.com", "type": "AAAA"}]),
            resolver,
            "2026-06-06T12:00:00+00:00",
        )

        self.assertEqual([], scan["records"][0]["values"])
        self.assertIsNone(scan["records"][0]["error"])

    def test_compare_dns_scan_to_baseline_detects_no_drift(self):
        baseline = {
            "records": [
                {
                    "site": "example.com",
                    "name": "example.com",
                    "type": "A",
                    "values": ["192.0.2.1"],
                    "error": None,
                }
            ]
        }
        current = copy.deepcopy(baseline)

        self.assertEqual([], run.compare_dns_scan_to_baseline(current, baseline))

    def test_compare_dns_scan_to_baseline_detects_missing_baseline(self):
        variances = run.compare_dns_scan_to_baseline({"records": []}, None)

        self.assertEqual("missing_baseline", variances[0]["kind"])

    def test_compare_dns_scan_to_baseline_detects_missing_and_new_records(self):
        baseline = {
            "records": [
                {
                    "site": "example.com",
                    "name": "example.com",
                    "type": "A",
                    "values": ["192.0.2.1"],
                    "error": None,
                }
            ]
        }
        current = {
            "records": [
                {
                    "site": "example.com",
                    "name": "example.com",
                    "type": "MX",
                    "values": ["10 mail.example.com."],
                    "error": None,
                }
            ]
        }

        kinds = [
            variance["kind"]
            for variance in run.compare_dns_scan_to_baseline(current, baseline)
        ]

        self.assertEqual(["missing_record", "new_record"], kinds)

    def test_compare_dns_scan_to_baseline_detects_changed_values(self):
        baseline = {
            "records": [
                {
                    "site": "example.com",
                    "name": "example.com",
                    "type": "A",
                    "values": ["192.0.2.1"],
                    "error": None,
                }
            ]
        }
        current = {
            "records": [
                {
                    "site": "example.com",
                    "name": "example.com",
                    "type": "A",
                    "values": ["192.0.2.2"],
                    "error": None,
                }
            ]
        }

        variances = run.compare_dns_scan_to_baseline(current, baseline)

        self.assertEqual("changed_values", variances[0]["kind"])
        self.assertEqual(["192.0.2.1"], variances[0]["missing_values"])
        self.assertEqual(["192.0.2.2"], variances[0]["new_values"])

    def test_compare_dns_scan_to_baseline_detects_resolver_errors(self):
        baseline = {
            "records": [
                {
                    "site": "example.com",
                    "name": "example.com",
                    "type": "A",
                    "values": ["192.0.2.1"],
                    "error": None,
                }
            ]
        }
        current = {
            "records": [
                {
                    "site": "example.com",
                    "name": "example.com",
                    "type": "A",
                    "values": [],
                    "error": "DNS timeout",
                }
            ]
        }

        variances = run.compare_dns_scan_to_baseline(current, baseline)

        self.assertEqual("resolver_error", variances[0]["kind"])
        self.assertEqual("DNS timeout", variances[0]["message"])

    def test_dns_baseline_command_does_not_write_on_empty_or_wrong_confirmation(self):
        writes = []

        wrong_exit = run.run_dns_baseline_command(
            self.make_sites([{"name": "example.com", "type": "A"}]),
            resolver=lambda *_args, **_kwargs: ["192.0.2.1"],
            input_func=lambda _prompt: "WRONG",
            confirmation_code="ABC123",
            save_func=writes.append,
        )
        empty_exit = run.run_dns_baseline_command(
            self.make_sites([{"name": "example.com", "type": "A"}]),
            resolver=lambda *_args, **_kwargs: ["192.0.2.1"],
            input_func=lambda _prompt: "",
            confirmation_code="ABC123",
            save_func=writes.append,
        )

        self.assertEqual(1, wrong_exit)
        self.assertEqual(1, empty_exit)
        self.assertEqual([], writes)

    def test_dns_baseline_command_writes_on_matching_confirmation(self):
        writes = []

        exit_code = run.run_dns_baseline_command(
            self.make_sites([{"name": "example.com", "type": "A"}]),
            resolver=lambda *_args, **_kwargs: ["192.0.2.1"],
            input_func=lambda _prompt: "ABC123",
            confirmation_code="ABC123",
            save_func=writes.append,
        )

        self.assertEqual(0, exit_code)
        self.assertEqual(1, len(writes))
        self.assertEqual(["192.0.2.1"], writes[0]["records"][0]["values"])


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


class DisabledSiteTrackingTests(unittest.TestCase):
    def setUp(self):
        self.original_print = run.print if hasattr(run, "print") else print
        run.print = lambda *args, **kwargs: None
        self.addCleanup(setattr, run, "print", self.original_print)
        self.now = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)

    def test_update_disabled_site_tracking_starts_newly_disabled_site(self):
        saved_state = {}
        self.patch_tracking_io({"disabled_site_tracking": {}}, saved_state)

        updated, reminders = run.update_disabled_site_tracking(
            self.make_sites({"example.com": False}), self.now
        )

        self.assertEqual([], reminders)
        self.assertEqual(
            "2026-06-06T12:00:00+00:00",
            updated["disabled_site_tracking"]["example.com"]["first_seen_disabled"],
        )
        self.assertEqual(updated, saved_state)

    def test_disabled_site_tracking_does_not_remind_before_six_hours(self):
        saved_state = {}
        self.patch_tracking_io(
            {
                "disabled_site_tracking": {
                    "example.com": {
                        "first_seen_disabled": "2026-06-06T06:01:00+00:00",
                        "last_seen_disabled": "2026-06-06T11:59:00+00:00",
                        "last_reminder_sent": None,
                    }
                }
            },
            saved_state,
        )

        _updated, reminders = run.update_disabled_site_tracking(
            self.make_sites({"example.com": False}), self.now
        )

        self.assertEqual([], reminders)
        self.assertIsNone(
            saved_state["disabled_site_tracking"]["example.com"]["last_reminder_sent"]
        )

    def test_disabled_site_tracking_reminds_at_six_hours(self):
        saved_state = {}
        self.patch_tracking_io(
            {
                "disabled_site_tracking": {
                    "example.com": {
                        "first_seen_disabled": "2026-06-06T06:00:00+00:00",
                        "last_seen_disabled": "2026-06-06T11:59:00+00:00",
                        "last_reminder_sent": None,
                    }
                }
            },
            saved_state,
        )

        _updated, reminders = run.update_disabled_site_tracking(
            self.make_sites({"example.com": False}), self.now
        )

        self.assertEqual(1, len(reminders))
        self.assertEqual("example.com", reminders[0]["site"])
        self.assertEqual(360, reminders[0]["disabled_minutes"])
        self.assertEqual(
            "2026-06-06T12:00:00+00:00",
            saved_state["disabled_site_tracking"]["example.com"]["last_reminder_sent"],
        )

    def test_disabled_site_tracking_waits_six_hours_between_reminders(self):
        saved_state = {}
        self.patch_tracking_io(
            {
                "disabled_site_tracking": {
                    "example.com": {
                        "first_seen_disabled": "2026-06-06T00:00:00+00:00",
                        "last_seen_disabled": "2026-06-06T11:59:00+00:00",
                        "last_reminder_sent": "2026-06-06T06:01:00+00:00",
                    }
                }
            },
            saved_state,
        )

        _updated, reminders = run.update_disabled_site_tracking(
            self.make_sites({"example.com": False}), self.now
        )

        self.assertEqual([], reminders)
        self.assertEqual(
            "2026-06-06T06:01:00+00:00",
            saved_state["disabled_site_tracking"]["example.com"]["last_reminder_sent"],
        )

    def test_disabled_site_tracking_repeats_after_another_six_hours(self):
        saved_state = {}
        self.patch_tracking_io(
            {
                "disabled_site_tracking": {
                    "example.com": {
                        "first_seen_disabled": "2026-06-06T00:00:00+00:00",
                        "last_seen_disabled": "2026-06-06T11:59:00+00:00",
                        "last_reminder_sent": "2026-06-06T06:00:00+00:00",
                    }
                }
            },
            saved_state,
        )

        _updated, reminders = run.update_disabled_site_tracking(
            self.make_sites({"example.com": False}), self.now
        )

        self.assertEqual(1, len(reminders))
        self.assertEqual("example.com", reminders[0]["site"])
        self.assertEqual(
            "2026-06-06T12:00:00+00:00",
            saved_state["disabled_site_tracking"]["example.com"]["last_reminder_sent"],
        )

    def test_disabled_site_tracking_clears_reenabled_sites(self):
        saved_state = {}
        self.patch_tracking_io(
            {
                "disabled_site_tracking": {
                    "example.com": {
                        "first_seen_disabled": "2026-06-06T06:00:00+00:00",
                        "last_seen_disabled": "2026-06-06T11:59:00+00:00",
                        "last_reminder_sent": None,
                    }
                }
            },
            saved_state,
        )

        updated, reminders = run.update_disabled_site_tracking(
            self.make_sites({"example.com": True}), self.now
        )

        self.assertEqual([], reminders)
        self.assertEqual({}, updated["disabled_site_tracking"])
        self.assertEqual({}, saved_state["disabled_site_tracking"])

    def test_disabled_site_tracking_removes_sites_no_longer_configured(self):
        saved_state = {}
        self.patch_tracking_io(
            {
                "disabled_site_tracking": {
                    "removed.example": {
                        "first_seen_disabled": "2026-06-06T06:00:00+00:00",
                        "last_seen_disabled": "2026-06-06T11:59:00+00:00",
                        "last_reminder_sent": None,
                    }
                }
            },
            saved_state,
        )

        updated, reminders = run.update_disabled_site_tracking(
            self.make_sites({"example.com": True}), self.now
        )

        self.assertEqual([], reminders)
        self.assertEqual({}, updated["disabled_site_tracking"])
        self.assertEqual({}, saved_state["disabled_site_tracking"])

    def test_update_incident_tracking_reset_preserves_disabled_site_tracking(self):
        original_state = {
            "incident_active": True,
            "incident_start": "2026-06-06T10:00:00+00:00",
            "incident_last_seen": "2026-06-06T10:05:00+00:00",
            "incident_duration": "5m 0s",
            "failures_total": 3,
            "disabled_site_tracking": {
                "example.com": {
                    "first_seen_disabled": "2026-06-06T06:00:00+00:00",
                    "last_seen_disabled": "2026-06-06T11:59:00+00:00",
                    "last_reminder_sent": None,
                }
            },
        }
        saved_state = {}
        self.patch_tracking_io(original_state, saved_state)

        updated = run.update_incident_tracking(0)

        self.assertEqual(
            original_state["disabled_site_tracking"],
            updated["disabled_site_tracking"],
        )
        self.assertEqual(updated, saved_state)

    def make_sites(self, checks_by_site):
        return {
            "sites": {
                site: {
                    "check": should_check,
                    "endpoints": {"/": {"status": 200, "dom_contains": ""}},
                }
                for site, should_check in checks_by_site.items()
            }
        }

    def patch_tracking_io(self, original_state, saved_state):
        self.addCleanup(setattr, run, "load_tracking", run.load_tracking)
        self.addCleanup(setattr, run, "save_tracking", run.save_tracking)
        run.load_tracking = lambda: copy.deepcopy(original_state)
        run.save_tracking = lambda data: saved_state.update(copy.deepcopy(data))


class AlertCadenceTests(unittest.TestCase):
    def test_alert_email_cadence_suppresses_short_incidents(self):
        self.assertFalse(run.should_send_alert_email(0))
        self.assertFalse(run.should_send_alert_email(4))

    def test_alert_email_cadence_sends_between_five_and_twenty_nine_minutes(self):
        self.assertTrue(run.should_send_alert_email(5))
        self.assertTrue(run.should_send_alert_email(29))

    def test_alert_email_cadence_sends_every_fifteen_minutes_after_thirty(self):
        self.assertTrue(run.should_send_alert_email(30))
        self.assertFalse(run.should_send_alert_email(31))
        self.assertTrue(run.should_send_alert_email(45))

    def test_force_email_overrides_cadence(self):
        self.assertTrue(run.should_send_alert_email(0, force_email=True))


class EscalationRoutingTests(unittest.TestCase):
    def make_parser(self, escalation_after_minutes=None):
        parser = configparser.ConfigParser()
        parser["DEFAULT"] = {
            "ALERTS_EMAIL": "alerts@example.com",
            "ESCALATION_EMAIL": "escalation@example.com",
        }
        if escalation_after_minutes is not None:
            parser["DEFAULT"]["ESCALATION_AFTER_MINUTES"] = str(escalation_after_minutes)
        return parser

    def test_escalation_threshold_defaults_to_five_hours(self):
        self.assertEqual(300, run.get_escalation_after_minutes(self.make_parser()))

    def test_escalation_threshold_can_be_configured(self):
        self.assertEqual(60, run.get_escalation_after_minutes(self.make_parser(60)))

    def test_invalid_escalation_threshold_uses_default(self):
        self.assertEqual(
            300, run.get_escalation_after_minutes(self.make_parser("not-a-number"))
        )

    def test_alerts_go_to_alerts_email_before_threshold(self):
        recipient, threshold = run.get_alert_recipient(self.make_parser(), 299)

        self.assertEqual("alerts@example.com", recipient)
        self.assertEqual(300, threshold)

    def test_alerts_go_to_escalation_email_at_threshold(self):
        recipient, threshold = run.get_alert_recipient(self.make_parser(), 300)

        self.assertEqual("escalation@example.com", recipient)
        self.assertEqual(300, threshold)

    def test_configured_threshold_controls_escalation_boundary(self):
        parser = self.make_parser(60)

        before_recipient, before_threshold = run.get_alert_recipient(parser, 59)
        at_recipient, at_threshold = run.get_alert_recipient(parser, 60)

        self.assertEqual("alerts@example.com", before_recipient)
        self.assertEqual(60, before_threshold)
        self.assertEqual("escalation@example.com", at_recipient)
        self.assertEqual(60, at_threshold)

    def test_force_email_uses_alerts_email_even_after_escalation_threshold(self):
        recipient, threshold = run.get_alert_recipient(
            self.make_parser(), 999, force_email=True
        )

        self.assertEqual("alerts@example.com", recipient)
        self.assertEqual(300, threshold)


class CliParsingTests(unittest.TestCase):
    def test_parse_args_supports_safe_testing_flags(self):
        args = run.parse_args(
            ["--dry-run", "--force-email", "--force-certificate-check", "--log-level", "DEBUG"]
        )

        self.assertTrue(args.dry_run)
        self.assertTrue(args.force_email)
        self.assertTrue(args.force_certificate_check)
        self.assertEqual("DEBUG", args.log_level)

    def test_manual_text_documents_common_usage(self):
        manual = run.get_manual_text()

        self.assertIn("PythonMonitorScript manual", manual)
        self.assertIn("python run.py --dry-run --force-email", manual)
        self.assertIn("MAILGUN_PRIVATE_KEY", manual)
        self.assertIn("Forced heartbeat emails use ALERTS_EMAIL", manual)
        self.assertIn("Certificate behavior", manual)

    def test_missing_dependency_detection_includes_selenium_only_for_screenshots(self):
        original_requests = run.requests
        original_aiohttp = run.aiohttp
        original_proxy = run.CIMultiDictProxy
        original_webdriver = run.webdriver
        original_dns_resolver = run.dns_resolver
        run.requests = None
        run.aiohttp = None
        run.CIMultiDictProxy = ()
        run.webdriver = None
        run.dns_resolver = None
        self.addCleanup(setattr, run, "requests", original_requests)
        self.addCleanup(setattr, run, "aiohttp", original_aiohttp)
        self.addCleanup(setattr, run, "CIMultiDictProxy", original_proxy)
        self.addCleanup(setattr, run, "webdriver", original_webdriver)
        self.addCleanup(setattr, run, "dns_resolver", original_dns_resolver)

        without_screenshots = run.get_missing_dependencies(screenshots_enabled=False)
        with_screenshots = run.get_missing_dependencies(screenshots_enabled=True)
        with_dns = run.get_missing_dependencies(dns_enabled=True)

        self.assertIn("requests", without_screenshots)
        self.assertIn("aiohttp", without_screenshots)
        self.assertIn("multidict", without_screenshots)
        self.assertNotIn("selenium", without_screenshots)
        self.assertIn("selenium", with_screenshots)
        self.assertIn("dnspython", with_dns)


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

    def test_disabled_site_email_markup_identifies_site_duration_and_action(self):
        markup = run.get_disabled_site_email_markup(
            [
                {
                    "site": "example.com",
                    "first_seen_disabled": "2026-06-06T06:00:00+00:00",
                    "disabled_minutes": 360,
                }
            ]
        )

        self.assertIn("Site tracking is disabled", markup)
        self.assertIn("Monitoring disabled for example.com", markup)
        self.assertIn("Disabled duration:</strong> 6h 0m", markup)
        self.assertIn("&quot;check&quot;: true", markup)

    def test_disabled_site_email_markup_escapes_site_names(self):
        markup = run.get_disabled_site_email_markup(
            [
                {
                    "site": "<example.com>",
                    "first_seen_disabled": "2026-06-06T06:00:00+00:00",
                    "disabled_minutes": 360,
                }
            ]
        )

        self.assertIn("&lt;example.com&gt;", markup)
        self.assertNotIn("<example.com>", markup)

    def test_render_email_template_separates_current_and_cumulative_failures(self):
        html = run.render_email_template(
            "<strong>Endpoint:</strong> /id.txt<br>",
            current_failure_count=2,
            incident_observations=18,
            incident_start_timestamp_delta="8m 7s",
            incident_start_timestamp="2026-06-06T17:29:25+00:00",
        )

        self.assertIn("Current failing checks: 2", html)
        self.assertIn("Incident observations: 18", html)
        self.assertIn("<strong>Endpoint:</strong> /id.txt<br>", html)
        self.assertNotIn("{{current_failure_count}}", html)
        self.assertNotIn("{{incident_observations}}", html)

    def test_send_urgent_email_dry_run_does_not_post_to_mailgun(self):
        original_dry_run = run.DRY_RUN
        original_post = getattr(run.requests, "post", None)
        run.DRY_RUN = True
        run.requests.post = lambda *args, **kwargs: self.fail("Mailgun post should not run")

        self.addCleanup(setattr, run, "DRY_RUN", original_dry_run)
        if original_post is None:
            self.addCleanup(delattr, run.requests, "post")
        else:
            self.addCleanup(setattr, run.requests, "post", original_post)

        response = run.send_urgent_email(
            "body",
            current_failure_count=1,
            incident_start_timestamp_delta="0m 0s",
            incident_start_timestamp="2026-06-06T17:29:25+00:00",
            to_address="admin@example.com",
            incident_observations=3,
        )

        self.assertIsNone(response)

    def test_dry_run_skips_response_body_artifact(self):
        original_dry_run = run.DRY_RUN
        run.DRY_RUN = True
        self.addCleanup(setattr, run, "DRY_RUN", original_dry_run)

        self.assertIsNone(run.save_html_to_file("nonce", "<html></html>"))

    def test_dry_run_skips_screenshot_even_without_browser(self):
        original_dry_run = run.DRY_RUN
        original_browser = run.BROWSER
        run.DRY_RUN = True
        run.BROWSER = None
        self.addCleanup(setattr, run, "DRY_RUN", original_dry_run)
        self.addCleanup(setattr, run, "BROWSER", original_browser)

        run.take_endpoint_screenshot("nonce", "https://example.com")


if __name__ == "__main__":
    unittest.main()
