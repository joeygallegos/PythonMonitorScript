#!/usr/bin/env python3
import argparse
import json
import html
import logging
import configparser
import os
import socket
import ssl
import time
import uuid
import asyncio
import sys
from math import ceil
from datetime import datetime, timezone

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    requests = None

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    from multidict import CIMultiDictProxy
except ImportError:
    CIMultiDictProxy = ()

try:
    from selenium import webdriver
except ImportError:
    webdriver = None

scriptdir = os.path.dirname(os.path.abspath(__file__))
os.chdir(scriptdir)

# These module-level collections are populated during a single script run.
# The script is executed repeatedly by cron, so anything that must survive
# between runs needs to be stored in tracking.json instead.
ALERTS = []
PARSER = configparser.ConfigParser()
BROWSER = None
SCREENSHOTS = []
SHOW_HEADERS = "--show-headers" in sys.argv
TAKE_SCREENSHOT = "--take-screenshot" in sys.argv
CERTIFICATE_EXPIRY_WARNING_DAYS = 10
# Heartbeat escalation is intentionally much slower than normal alerting.
# ALERTS_EMAIL gets the early/noisy notifications first, then an unresolved
# incident moves to ESCALATION_EMAIL after this many minutes unless config.ini
# overrides the value.
DEFAULT_ESCALATION_AFTER_MINUTES = 300
DRY_RUN = False
FORCE_EMAIL = False
FORCE_CERTIFICATE_CHECK = False
LOGGER = logging.getLogger("PythonMonitorScript")


def parse_args(argv=None):
    # Keep command-line options centralized so cron/manual execution behavior is
    # explicit and tests can parse options without running the monitor.
    parser = argparse.ArgumentParser(
        description="Monitor website health checks and HTTPS certificate renewals.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python run.py
      Run normal heartbeat checks, update tracking.json, and send emails by cadence.

  python run.py --dry-run --force-email
      Exercise check and email rendering paths without writing state or posting to Mailgun.

  python run.py --force-certificate-check
      Run the daily certificate pass even if it already ran today.

Required config.ini keys:
  MAILGUN_PRIVATE_KEY, MAILGUN_DOMAIN, MAILGUN_FROM, ALERTS_EMAIL,
  ESCALATION_EMAIL, SCREENSHOTS_ENABLED, TMP_PATH_SCREENSHOTS, DEBUG

Optional config.ini keys:
  ESCALATION_AFTER_MINUTES defaults to 300 if omitted.

Sites are configured in sites.json. Domains with "check": true are checked.
""",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Print the full built-in manual and exit.",
    )
    parser.add_argument(
        "--show-headers",
        action="store_true",
        help="Include response headers and body content in alert emails.",
    )
    parser.add_argument(
        "--take-screenshot",
        action="store_true",
        help="Capture Selenium screenshots for failed checks when enabled in config.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run checks without writing tracking.json, deleting artifacts, or sending email.",
    )
    parser.add_argument(
        "--force-email",
        action="store_true",
        help="Send alert email whenever current failures exist, ignoring normal cadence.",
    )
    parser.add_argument(
        "--force-certificate-check",
        action="store_true",
        help="Run certificate checks even if they already ran today.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console log verbosity.",
    )
    return parser.parse_args(argv)


def get_manual_text():
    return """PythonMonitorScript manual

Purpose
  Monitor configured HTTP or HTTPS endpoints and send Mailgun alerts when
  checks fail. Also checks enabled HTTPS domains' certificate expiry once per
  UTC day.

Normal usage
  python run.py

  Intended cron usage is once per minute. Heartbeat checks run every execution.
  Certificate checks are gated by tracking.json and run once per UTC day.

Testing usage
  python run.py --dry-run
      Run checks without writing tracking.json, creating response artifacts, or
      sending Mailgun email.

  python run.py --dry-run --force-email
      Force the email-rendering path for current heartbeat failures without
      posting to Mailgun. Forced heartbeat emails use ALERTS_EMAIL so testing
      does not notify the escalation recipient.

  python run.py --force-email
      Send heartbeat alert email whenever failures exist, ignoring the normal
      5-minute delay and 15-minute reminder cadence. The forced email is sent
      to ALERTS_EMAIL even when the stored incident is already past the
      escalation threshold.

  python run.py --force-certificate-check
      Run certificate checks even when certificate_last_checked_date is today.

Debug options
  --show-headers
      Include response headers and body content in heartbeat alert emails.

  --take-screenshot
      Capture Selenium screenshots for failed checks when SCREENSHOTS_ENABLED
      is true in config.ini.

  --log-level DEBUG|INFO|WARNING|ERROR
      Control console log verbosity.

Config files
  config.ini
      Mailgun credentials, recipient emails, screenshot behavior, and debug mode.
      Required keys: MAILGUN_PRIVATE_KEY, MAILGUN_DOMAIN, MAILGUN_FROM,
      ALERTS_EMAIL, ESCALATION_EMAIL, SCREENSHOTS_ENABLED,
      TMP_PATH_SCREENSHOTS, DEBUG.
      Optional keys: ESCALATION_AFTER_MINUTES defaults to 300.

  sites.json
      Domains, optional scheme ("https" by default, or "http"), endpoint paths,
      expected status codes, and optional DOM strings.

  tracking.json
      Persistent incident state and daily certificate scheduling state.

Email behavior
  Heartbeat emails are suppressed for the first 5 minutes of an incident.
  From 5 through 29 minutes, emails send every run. From 30 minutes onward,
  reminders send every 15 minutes. Alerts go to ALERTS_EMAIL until the
  incident reaches ESCALATION_AFTER_MINUTES, then ESCALATION_EMAIL is used.

Certificate behavior
  HTTPS certificates alert when expiry is 10 days away or less, or when the
  certificate cannot be fetched or parsed. HTTP sites skip certificate checks.
"""


def setup_logging(level="INFO"):
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def get_missing_dependencies(screenshots_enabled=False):
    # Keep --manual usable even before dependencies are installed, but fail
    # clearly before running monitor work that needs those packages.
    missing = []
    if requests is None:
        missing.append("requests")
    if aiohttp is None:
        missing.append("aiohttp")
    if CIMultiDictProxy == ():
        missing.append("multidict")
    if screenshots_enabled and webdriver is None:
        missing.append("selenium")
    return missing


def get_escalation_after_minutes(parser):
    # Keep the historical five-hour escalation default, but allow config.ini to
    # make the high threshold explicit for different deployments. A missing or
    # malformed value falls back to the default instead of crashing the monitor,
    # because alert delivery should keep working even after a config typo.
    try:
        return parser.getint("DEFAULT", "ESCALATION_AFTER_MINUTES")
    except (configparser.Error, ValueError):
        return DEFAULT_ESCALATION_AFTER_MINUTES


def get_alert_recipient(parser, duration_minutes, force_email=False):
    # Initial and ongoing non-escalated alerts always go to ALERTS_EMAIL. Only
    # incidents that remain unresolved past the high threshold move to
    # ESCALATION_EMAIL. --force-email is mainly a manual testing path, so keep
    # it pointed at ALERTS_EMAIL instead of unexpectedly paging escalation.
    escalation_after_minutes = get_escalation_after_minutes(parser)
    if force_email:
        # A forced email can be run against old tracking.json state. Without
        # this branch, testing a long-running incident would send to
        # ESCALATION_EMAIL, which is surprising when the operator only wants to
        # validate Mailgun/template behavior.
        return parser.get("DEFAULT", "ALERTS_EMAIL"), escalation_after_minutes
    if duration_minutes >= escalation_after_minutes:
        return parser.get("DEFAULT", "ESCALATION_EMAIL"), escalation_after_minutes
    return parser.get("DEFAULT", "ALERTS_EMAIL"), escalation_after_minutes


def now_iso():
    # Store persisted timestamps in UTC so cron runs from different machines or
    # daylight-saving changes do not make incident math inconsistent.
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_today_str():
    # Certificate checks are intentionally daily, even though heartbeat checks
    # run every minute. This UTC date is the daily gate key.
    return datetime.now(timezone.utc).date().isoformat()


def format_incident_start_pretty(iso_ts: str) -> str:
    if not iso_ts:
        return "Unknown"
    dt = datetime.fromisoformat(iso_ts)  # your string includes +00:00
    dt_local = dt.astimezone()  # converts to local timezone
    return dt_local.strftime("%b %d, %Y %I:%M %p %Z")  # "Jan 10, 2026 08:37 AM CST"


def load_tracking():
    # tracking.json is the durable state file for incident duration/counts and
    # once-per-day certificate check scheduling.
    path = os.path.join(scriptdir, "tracking.json")
    if not os.path.exists(path):
        # Default state for a first run or a missing tracking file.
        return {
            "incident_active": False,
            "incident_start": None,
            "incident_last_seen": None,
            "incident_duration": "0s",
            "failures_total": 0,
            "certificate_last_checked_date": None,
            "certificate_last_alerted_date": None,
        }

    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        # Returning an empty dict lets callers continue with .get() defaults,
        # but this should still be visible in cron output.
        print(f"Error loading tracking file: {e}")
        return {}


def save_tracking(data: dict):
    # All persistent monitor state writes flow through this helper.
    if DRY_RUN:
        LOGGER.info("DRY RUN: skipping tracking.json write")
        LOGGER.debug("DRY RUN tracking payload: %s", json.dumps(data, indent=2))
        return

    path = os.path.join(scriptdir, "tracking.json")
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        LOGGER.error("Error writing tracking file: %s", e)


def update_incident_tracking(alert_count: int):
    """
    Adjusts incident tracking info based on number of alerts this run.
    Starts or ends an incident as needed.
    """
    tracking = load_tracking()
    now = now_iso()

    if alert_count > 0:
        # Any current-run alert means the incident is active. A brand-new
        # incident records the start time; an existing incident preserves it.
        if not tracking.get("incident_active"):
            print("⚠️ Incident STARTING")
            tracking["incident_active"] = True
            tracking["incident_start"] = now
            tracking["failures_total"] = alert_count
        else:
            # failures_total is cumulative across cron observations, not just
            # the number of currently failing checks in this run.
            tracking["failures_total"] += alert_count

        tracking["incident_last_seen"] = now

        # Recompute the incident duration from the original start timestamp.
        start_dt = datetime.fromisoformat(tracking["incident_start"]).replace(
            tzinfo=timezone.utc
        )
        duration = datetime.now(timezone.utc) - start_dt
        mins, secs = divmod(duration.total_seconds(), 60)
        tracking["incident_duration"] = f"{int(mins)}m {int(secs)}s"

        save_tracking(tracking)
        return tracking

    else:
        # No current-run alerts means the heartbeat incident has cleared.
        if tracking.get("incident_active"):
            print("✅ Incident CLEARED")

        # Reset uptime incident state while preserving unrelated monitor state.
        tracking = {
            "incident_active": False,
            "incident_start": None,
            "incident_last_seen": None,
            "incident_duration": "0s",
            "failures_total": 0,
            "certificate_last_checked_date": tracking.get(
                "certificate_last_checked_date"
            ),
            "certificate_last_alerted_date": tracking.get(
                "certificate_last_alerted_date"
            ),
        }
        save_tracking(tracking)
        return tracking


def get_website_dictionary():
    # sites.json is the source of truth for domains and endpoint expectations.
    sites_config_file = open(os.path.join(scriptdir, "sites.json"))
    sites_to_monitor = json.load(sites_config_file)
    return sites_to_monitor


def get_site_scheme(site_config):
    # Existing site entries predate this option, so HTTPS remains the default.
    scheme = str(site_config.get("scheme", "https")).lower().strip()
    if scheme not in ("http", "https"):
        raise ValueError(f"Unsupported site scheme: {scheme}")
    return scheme


def build_endpoint_url(sites, site, endpoint):
    scheme = get_site_scheme(sites["sites"][site])
    return f"{scheme}://{site}{endpoint}"


def uses_https(site_config):
    return get_site_scheme(site_config) == "https"


def take_endpoint_screenshot(nonce=str, endpoint=str):
    # Screenshots are optional and only useful when Selenium is enabled. The
    # nonce ties the screenshot attachment back to a specific alert in email.
    if DRY_RUN:
        LOGGER.info("DRY RUN: skipping screenshot for %s", endpoint)
        return

    path = PARSER.get("DEFAULT", "TMP_PATH_SCREENSHOTS")

    # Filename based on path and nonce
    filename = f"{path}/screenshot_{nonce}.png"

    try:
        BROWSER.get(endpoint)
        time.sleep(2)  # Adjust the sleep duration based on your requirements

        BROWSER.save_screenshot(filename)
        SCREENSHOTS.append((nonce, filename))
    except Exception as e:
        LOGGER.error("Error accessing %s for screenshot: %s", endpoint, e)
        if BROWSER is not None:
            BROWSER.save_screenshot(filename)
            SCREENSHOTS.append((nonce, filename))


async def do_endpoint_check(sites, site, endpoint):
    # Checks one configured endpoint for both response status and an
    # optional expected substring in the response body.
    endpoint_url = build_endpoint_url(sites, site, endpoint)
    print(
        "- Checking endpoint "
        + str(endpoint)
        + " at "
        + endpoint_url
        + " for a status code "
        + str(sites["sites"][site]["endpoints"][endpoint]["status"])
    )

    timeout = aiohttp.ClientTimeout(total=5)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 PythonMonitorScript/1.0",
        "Accept": "*/*",
        "Connection": "close",
    }

    try:
        # A fresh session is currently created per endpoint. This works, but is
        # less efficient than sharing one session across all endpoint checks.
        async with aiohttp.ClientSession() as session:
            async with session.get(
                endpoint_url, timeout=timeout, headers=headers
            ) as response:
                response_body = await response.text()
                response_headers = response.headers
                expected_status = int(
                    sites["sites"][site]["endpoints"][endpoint]["status"]
                )
                alert_raised = False

                if response.status != expected_status:
                    # Status mismatch alerts include response body and headers
                    # so the email/debug attachment can show what the server
                    # actually returned.
                    status_nonce = str(uuid.uuid4().int)[:16]
                    ALERTS.append(
                        {
                            "alert": {
                                "site": site,
                                "endpoint": endpoint,
                                "expected": expected_status,
                                "received": response.status,
                                "exception": "Status code mismatch",
                                "nonce": status_nonce,
                                "body": response_body,
                                "headers": response_headers,
                            }
                        }
                    )
                    alert_raised = True

                    if TAKE_SCREENSHOT and SCREENSHOTS_ENABLED:
                        take_endpoint_screenshot(status_nonce, endpoint_url)
                    html_path = save_html_to_file(status_nonce, response_body)
                    if html_path:
                        SCREENSHOTS.append((status_nonce, html_path))

                search_key = sites["sites"][site]["endpoints"][endpoint]["dom_contains"]
                if search_key and search_key not in response_body:
                    # dom_contains is optional. Empty strings are skipped; any
                    # non-empty configured value must appear in the response.
                    dom_nonce = str(uuid.uuid4().int)[:16]
                    ALERTS.append(
                        {
                            "alert": {
                                "site": site,
                                "endpoint": endpoint,
                                "expected": 0,
                                "received": 0,
                                "exception": "DOM string mismatch",
                                "nonce": dom_nonce,
                                "body": response_body,
                                "headers": response_headers,
                            }
                        }
                    )
                    alert_raised = True

                    if TAKE_SCREENSHOT and SCREENSHOTS_ENABLED:
                        take_endpoint_screenshot(dom_nonce, endpoint_url)
                    html_path = save_html_to_file(dom_nonce, response_body)
                    if html_path:
                        SCREENSHOTS.append((dom_nonce, html_path))

                if alert_raised:
                    print(
                        f"❌ Alert raised for {site}{endpoint} - Exception: {ALERTS[-1]['alert']['exception']}"
                    )
                else:
                    print(f"   ✅ Passed: {endpoint}")

    except Exception as ex:
        # Network errors, TLS failures, DNS failures, and read timeouts all land
        # here. They are represented as response code 0 in the email.
        message = str(ex) or "Unreachable, response code is 0"
        print("endpoint seems to be unreachable, response code is 0")
        print("exception: " + message)

        fallback_nonce = str(uuid.uuid4().int)[:16]
        ALERTS.append(
            {
                "alert": {
                    "site": site,
                    "endpoint": endpoint,
                    "expected": int(
                        sites["sites"][site]["endpoints"][endpoint]["status"]
                    ),
                    "received": 0,
                    "exception": message,
                    "nonce": fallback_nonce,
                    "body": None,
                    "headers": None,
                }
            }
        )

        if TAKE_SCREENSHOT and SCREENSHOTS_ENABLED:
            take_endpoint_screenshot(fallback_nonce, endpoint_url)


def do_heartbeat_check(sites):
    # Run all configured endpoint checks. This function currently steps through
    # each endpoint synchronously by repeatedly driving the event loop.
    print("do_heartbeat_check started")
    loop = asyncio.get_event_loop()
    for site in sites["sites"]:
        should_check = sites["sites"][site]["check"]
        if should_check:
            print("    ")
            print("Starting checks for " + get_site_scheme(sites["sites"][site]) + "://" + site)
            for endpoint in sites["sites"][site]["endpoints"]:
                loop.run_until_complete(do_endpoint_check(sites, site, endpoint))
        else:
            print("check variable set to false for " + site)

    print("do_heartbeat_check ended")


# Function to get the number of checks (endpoints) for a given site
def get_num_of_checks(site_name):
    # Check if the site exists in the config
    domains = get_website_dictionary()["sites"]
    if site_name in domains:
        # Return the number of endpoints (checks) for the given site
        return len(domains[site_name]["endpoints"])
    else:
        # If site does not exist in config, return 0 or a message
        return 0  # Or return an error message if preferred


def should_run_certificate_check(tracking=None, today=None):
    # The script may run every minute, but certificate checks are intentionally
    # limited to one run per UTC day.
    if tracking is None:
        tracking = load_tracking()
    if today is None:
        today = utc_today_str()
    return tracking.get("certificate_last_checked_date") != today


def update_certificate_tracking(checked_date, alerted=False):
    # Record that the daily certificate pass happened. If an alert email was
    # sent, also remember that date for future reporting/debugging.
    tracking = load_tracking()
    tracking["certificate_last_checked_date"] = checked_date
    if alerted:
        tracking["certificate_last_alerted_date"] = checked_date
    save_tracking(tracking)
    return tracking


def should_send_alert_email(duration_minutes, force_email=False):
    # Normal cadence suppresses very short incidents and reduces reminder noise.
    # The force flag is for deliberate manual testing.
    return force_email or 5 <= duration_minutes < 30 or (
        duration_minutes >= 30 and duration_minutes % 15 == 0
    )


def get_certificate_expiry(domain, port=443, timeout=5):
    # Open a real TLS connection with SNI so virtual-hosted certificates are
    # checked for the requested domain, not just the server's default cert.
    context = ssl.create_default_context()
    with socket.create_connection((domain, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=domain) as ssock:
            cert = ssock.getpeercert()

    not_after = cert.get("notAfter")
    if not not_after:
        # A cert without notAfter is not useful for expiry monitoring.
        raise ValueError("Certificate did not include a notAfter expiry date")

    # Python's stdlib certificate dict returns notAfter in this OpenSSL-style
    # string format, e.g. "Jun 16 12:00:00 2026 GMT".
    return datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(
        tzinfo=timezone.utc
    )


def certificate_days_remaining(expires_at, now=None):
    # Round up so a cert with 10 days and 1 second remaining displays as 11
    # days, while exactly-expired certs display as 0.
    if now is None:
        now = datetime.now(timezone.utc)
    seconds_remaining = (expires_at - now).total_seconds()
    return ceil(seconds_remaining / 86400)


def certificate_needs_alert(expires_at, now=None):
    # Alert when the remaining lifetime is at or below the configured threshold.
    # Expired certificates naturally satisfy this condition.
    if now is None:
        now = datetime.now(timezone.utc)
    return (expires_at - now).total_seconds() <= (
        CERTIFICATE_EXPIRY_WARNING_DAYS * 86400
    )


def do_certificate_checks(sites):
    # Check each enabled HTTPS domain's certificate. Certificate monitoring is
    # domain-level, not endpoint-level, and HTTP-only sites do not have certs.
    print("do_certificate_checks started")
    certificate_alerts = []

    for site, site_config in sites["sites"].items():
        if not site_config.get("check"):
            # Reuse the same site-level enable/disable switch as heartbeat
            # monitoring to avoid adding config complexity.
            print("certificate check skipped because check variable is false for " + site)
            continue

        if not uses_https(site_config):
            print("certificate check skipped because scheme is not https for " + site)
            continue

        print(f"- Checking certificate for {site}")
        try:
            expires_at = get_certificate_expiry(site)
            days_remaining = certificate_days_remaining(expires_at)
            if certificate_needs_alert(expires_at):
                print(
                    f"❌ Certificate alert raised for {site} - expires in {days_remaining} day(s)"
                )
                certificate_alerts.append(
                    {
                        "domain": site,
                        "expires_at": expires_at,
                        "days_remaining": days_remaining,
                        "exception": None,
                    }
                )
            else:
                print(
                    f"   ✅ Certificate passed: {site} expires in {days_remaining} day(s)"
                )
        except Exception as ex:
            # A failed certificate fetch can mean DNS, TCP, TLS, or certificate
            # validation trouble. Treat it as alert-worthy because users will
            # experience HTTPS failures too.
            message = str(ex) or "Unable to inspect certificate"
            print(f"❌ Certificate alert raised for {site} - Exception: {message}")
            certificate_alerts.append(
                {
                    "domain": site,
                    "expires_at": None,
                    "days_remaining": None,
                    "exception": message,
                }
            )

    print("do_certificate_checks ended")
    return certificate_alerts


def get_certificate_email_markup(certificate_alerts):
    # Build the alert-specific HTML fragment that gets injected into the shared
    # email-content.html template.
    html_body = (
        "Certificate monitoring detected a renewal warning.<br><br>"
        "<strong>Affected certificates</strong><br>"
    )

    for alert in certificate_alerts:
        # Escape values that can originate from config or exception text before
        # inserting them into an HTML email.
        domain = html.escape(str(alert["domain"]))
        html_body += f"<span style='color: #dc818f;'>Certificate warning for {domain}</span><br>"
        html_body += f"<strong>Domain:</strong> {domain}<br>"

        if alert["expires_at"]:
            html_body += (
                f"<strong>Expires:</strong> {alert['expires_at'].strftime('%b %d, %Y %I:%M %p UTC')}<br>"
            )
            html_body += (
                f"<strong>Days remaining:</strong> {alert['days_remaining']}<br>"
            )

        if alert["exception"]:
            html_body += (
                f"<strong>Exception:</strong> {html.escape(str(alert['exception']))}<br>"
            )

        html_body += "<br>"

    html_body += (
        "<strong>Action required</strong><br>"
        "Renew or repair the affected certificate before it expires.<br>"
    )
    return html_body


def save_html_to_file(nonce: str, html: str) -> str:
    # Save the raw response body for failed checks. These files can be attached
    # to emails when screenshots/debug attachments are enabled.
    if DRY_RUN:
        LOGGER.info("DRY RUN: skipping response body artifact for nonce %s", nonce)
        return None

    path = PARSER.get("DEFAULT", "TMP_PATH_SCREENSHOTS")
    filename = os.path.join(path, f"response_{nonce}.txt")
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        return filename
    except Exception as e:
        print(f"Error saving HTML for nonce {nonce}: {e}")
        return None


def get_email_markup():
    # Build the heartbeat-specific HTML fragment injected into the shared email
    # template. ALERTS may contain multiple endpoint failures for multiple sites.
    print("get_email_markup started")

    html_body = ""

    # Step 1: Group the alerts by "site"
    grouped_alerts = {}
    for alert in ALERTS:
        site = alert["alert"]["site"]
        if site not in grouped_alerts:
            grouped_alerts[site] = []
        grouped_alerts[site].append(alert)

    # Step 2: Iterate through the grouped alerts
    for site, alerts in grouped_alerts.items():
        # Count the number of failed checks
        # Multiple alerts for the same endpoint count as one failed check in the
        # per-site summary.
        unique_failures = {a["alert"]["endpoint"] for a in alerts}
        failed_checks = len(unique_failures)

        # Get the total number of checks for the domain (from config)
        total_checks_for_domain = get_num_of_checks(site)

        # Header message
        html_body += f"<span style='color: #dc818f;'>{failed_checks} of {total_checks_for_domain} checks failed for {site}</span><br>"

        # Step 3: Loop through each alert for this site
        for alert in alerts:
            html_body += f"<strong>Endpoint:</strong> {alert['alert']['endpoint']} <br>"
            html_body += (
                f"<strong>Response Code:</strong> {alert['alert']['received']} <br>"
            )

            if alert["alert"]["exception"]:
                html_body += (
                    f"<strong>Exception:</strong> {alert['alert']['exception']} <br>"
                )

            # Debug nonce
            html_body += f"<strong>Nonce:</strong> {alert['alert']['nonce']} <br>"

            if SCREENSHOTS_ENABLED is True:
                # Inline pictures from Selenium using the nonce as part of the image CID
                html_body += f"<strong>Screenshot:</strong><br><img src='cid:{alert['alert']['nonce']}.png' alt='Nonce Image'><br>"

            # Optionally, add headers if they exist
            if SHOW_HEADERS:
                if alert["alert"]["headers"] and isinstance(
                    alert["alert"]["headers"], CIMultiDictProxy
                ):
                    html_body += "<strong>Headers:</strong><br>"
                    header_data = alert["alert"]["headers"]
                    for key, value in header_data.items():
                        html_body += f"- <strong><i>{key}:</i></strong> {value} <br>"

                if alert["alert"]["body"]:
                    html_body += (
                        f"<strong>Body:</strong> {str(alert['alert']['body'])} <br>"
                    )
            html_body += "<br>"
        # Optionally, add a separator for each site's alerts
        html_body += "<hr><br>"

    return html_body


def render_email_template(
    html_body,
    current_failure_count=0,
    incident_observations=0,
    incident_start_timestamp_delta=str,
    incident_start_timestamp=str,
):
    # The shared template uses both current failures and cumulative incident
    # observations so alert emails do not imply every observed failure is still
    # active right now.
    with open(os.path.join(scriptdir, "email-content.html")) as template_file:
        html_template = template_file.read()
    html_template = str(html_template).replace("{{replace_alerts}}", html_body)
    html_template = str(html_template).replace(
        "{{current_failure_count}}", str(current_failure_count)
    )
    html_template = str(html_template).replace(
        "{{incident_observations}}", str(incident_observations)
    )
    html_template = str(html_template).replace(
        "{{failure_count}}", str(incident_observations)
    )
    html_template = str(html_template).replace(
        "{{incident_start_timestamp_delta}}", str(incident_start_timestamp_delta)
    )
    html_template = str(html_template).replace(
        "{{incident_start_timestamp_pretty}}",
        format_incident_start_pretty(str(incident_start_timestamp)),
    )
    return html_template


def send_urgent_email(
    html_body,
    current_failure_count=0,
    incident_start_timestamp_delta=str,
    incident_start_timestamp=str,
    to_address=str,
    subject="URGENT NOTIFICATION - PythonMonitorScript",
    incident_observations=None,
):
    # Render the shared HTML template and post it to Mailgun. This function is
    # used for both heartbeat alerts and certificate alerts.
    LOGGER.info("Preparing email to %s", to_address)
    if incident_observations is None:
        incident_observations = current_failure_count

    LOGGER.debug("Rendering email template")
    html_template = render_email_template(
        html_body,
        current_failure_count,
        incident_observations,
        incident_start_timestamp_delta,
        incident_start_timestamp,
    )

    files = []
    added_filenames = set()

    for nonce, filename in SCREENSHOTS:
        # Attach each generated file once. PNG files are inline images; saved
        # response bodies are normal text attachments.
        if not filename or filename in added_filenames:
            continue
        added_filenames.add(filename)

        try:
            with open(filename, "rb") as file_obj:
                file_content = file_obj.read()
                file_ext = os.path.splitext(filename)[1].lower()

                if file_ext == ".png":
                    files.append(("inline", (f"{nonce}.png", file_content)))
                elif file_ext == ".txt":
                    files.append(("attachment", (f"{nonce}.txt", file_content)))
        except FileNotFoundError:
            LOGGER.warning("Email attachment file not found: %s", filename)
        except Exception as e:
            LOGGER.error("Error reading email attachment %s: %s", filename, e)

    if DRY_RUN:
        LOGGER.info("DRY RUN: skipping Mailgun post to %s", to_address)
        LOGGER.info("DRY RUN email subject: %s", subject)
        LOGGER.debug("DRY RUN email html: %s", html_template)
        return None

    LOGGER.info("Posting request to Mailgun for %s", to_address)
    email_post = requests.post(
        f"https://api.mailgun.net/v3/{PARSER.get('DEFAULT', 'MAILGUN_DOMAIN')}/messages",
        auth=("api", PARSER.get("DEFAULT", "MAILGUN_PRIVATE_KEY")),
        files=files,
        data={
            "from": PARSER.get("DEFAULT", "MAILGUN_FROM"),
            "to": [to_address],
            "subject": subject,
            "html": html_template,
        },
    )
    if email_post.status_code >= 400:
        LOGGER.error("Mailgun returned %s: %s", email_post.status_code, email_post.text)
    else:
        LOGGER.info("Mailgun returned %s: %s", email_post.status_code, email_post.text)
    LOGGER.debug("Mailgun response headers: %s", email_post.headers)
    return email_post


# get json object from the file
def read_data_from_manifest():
    tracking_manifest_file = open(os.path.join(scriptdir, "tracking.json"))
    manifest = json.load(tracking_manifest_file)
    return manifest


# write JSON object to the file
def write_data_to_manifest(new_data):
    try:
        manifest_path = os.path.join(scriptdir, "tracking.json")

        with open(manifest_path, "w") as tracking_file:
            json.dump(new_data, tracking_file, indent=2)

    except Exception as ex:
        print("write_data_to_manifest exception:", ex)


# get failed ticks from file storage
def get_failed_ticks():
    tracking_file_path = os.path.join(scriptdir, "tracking.json")

    try:
        with open(tracking_file_path) as tracking_file:
            current_json_tracking = json.load(tracking_file)
            failed_ticks = int(current_json_tracking.get("failed_count", 0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        # Handle file not found, JSON decode error, or invalid value gracefully
        print(f"Error reading tracking file: {e}")
        failed_ticks = 0

    return max(0, failed_ticks)


#  write failed_count to the manifest file
def set_failed_ticks(count=0):
    # get object data from read_data_from_manifest()
    manifest_tmp_data = read_data_from_manifest()

    # replace entry with new count
    manifest_tmp_data["failed_count"] = count

    write_data_to_manifest(manifest_tmp_data)


# manipulate manifest array and trigger a write to the manifest file
def set_incident_start_timestamp(new_timestamp=str):
    json_data = read_data_from_manifest()
    json_data["incident_start_timestamp"] = new_timestamp
    write_data_to_manifest(json_data)


def get_incident_start_timestamp():
    return str(read_data_from_manifest()["incident_start_timestamp"])


def get_pretty_time(then, now=datetime.now(), interval="default"):
    # Returns a duration as specified by variable interval
    # Functions, except totalDuration, returns [quotient, remainder]

    duration = now - then  # For build-in functions
    duration_in_s = duration.total_seconds()

    def years():
        return divmod(duration_in_s, 31536000)  # Seconds in a year=31536000.

    def days(seconds=None):
        return divmod(
            seconds if seconds != None else duration_in_s, 86400
        )  # Seconds in a day = 86400

    def hours(seconds=None):
        return divmod(
            seconds if seconds != None else duration_in_s, 3600
        )  # Seconds in an hour = 3600

    def minutes(seconds=None):
        return divmod(
            seconds if seconds != None else duration_in_s, 60
        )  # Seconds in a minute = 60

    def seconds(seconds=None):
        if seconds != None:
            return divmod(seconds, 1)
        return duration_in_s

    def totalDuration():
        y = years()
        d = days(y[1])  # Use remainder to calculate next variable
        h = hours(d[1])
        m = minutes(h[1])
        s = seconds(m[1])

        return "{}h {}m {}s".format(int(h[0]), int(m[0]), int(s[0]))

    return {
        "years": int(years()[0]),
        "days": int(days()[0]),
        "hours": int(hours()[0]),
        "minutes": int(minutes()[0]),
        "seconds": int(seconds()),
        "default": totalDuration(),
    }[interval]


if __name__ == "__main__":
    # Runtime entrypoint for cron/manual execution. Importing run.py for tests
    # should not execute this block.
    args = parse_args()
    if args.manual:
        print(get_manual_text())
        sys.exit(0)

    setup_logging(args.log_level)
    SHOW_HEADERS = args.show_headers
    TAKE_SCREENSHOT = args.take_screenshot
    DRY_RUN = args.dry_run
    FORCE_EMAIL = args.force_email
    FORCE_CERTIFICATE_CHECK = args.force_certificate_check

    LOGGER.info("Reading data from config.ini")
    PARSER.read("config.ini")
    SCREENSHOTS_ENABLED = PARSER.getboolean("DEFAULT", "SCREENSHOTS_ENABLED")
    missing_dependencies = get_missing_dependencies(SCREENSHOTS_ENABLED)
    if missing_dependencies:
        LOGGER.error(
            "Missing required Python packages: %s. Install with: pip install -r requirements.txt",
            ", ".join(missing_dependencies),
        )
        sys.exit(1)

    websites = get_website_dictionary()

    # Launch browser if screenshot capture is enabled
    if SCREENSHOTS_ENABLED and not DRY_RUN:
        # Selenium is initialized only when screenshot capture is enabled,
        # because launching Chrome is relatively expensive and unnecessary for
        # normal checks.
        options = webdriver.ChromeOptions()
        if not PARSER.getboolean("DEFAULT", "DEBUG"):
            options.add_argument("--headless")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        BROWSER = webdriver.Chrome(options=options)

    # Run endpoint checks
    # Heartbeat checks run every script execution. With the usual cron setup,
    # that means every minute.
    do_heartbeat_check(websites)

    # === POST-CHECK HANDLING ===
    if ALERTS:
        # Current-run failures update or start the active incident.
        print(f"🔥 ALERTS detected: {len(ALERTS)} issue(s) found")
        tracking_info = update_incident_tracking(len(ALERTS))
        duration_str = tracking_info["incident_duration"]
        duration_minutes = int(duration_str.split("m")[0])
        # Recipient selection is separate from email cadence. The cadence
        # decides whether this run should send anything; recipient selection
        # decides whether that send is still a normal alert or has become an
        # escalation. FORCE_EMAIL keeps recipient selection on ALERTS_EMAIL so
        # manual test sends do not notify the escalation contact.
        to_email, escalation_after_minutes = get_alert_recipient(
            PARSER, duration_minutes, force_email=FORCE_EMAIL
        )

        if duration_minutes >= escalation_after_minutes and not FORCE_EMAIL:
            # For normal cron runs, a long-running incident has exceeded the
            # configured unresolved threshold, so future reminder emails route
            # to ESCALATION_EMAIL until the incident clears.
            print(
                f"🚨 Escalation threshold reached ({escalation_after_minutes}+ minutes). Switching to escalation email."
            )
        elif duration_minutes >= escalation_after_minutes and FORCE_EMAIL:
            # The incident may already be old in tracking.json, but this run is
            # explicitly a manual force-send. Keep it on ALERTS_EMAIL and make
            # that visible in console output.
            print(
                "📧 Force email enabled. Sending test alert to ALERTS_EMAIL instead of escalation recipient."
            )

        # Email schedule rules: suppress emails for the first few minutes to
        # avoid noisy one-off failures. After 30 minutes, send reminders every
        # 15 minutes. --force-email intentionally bypasses this for testing.
        should_email = should_send_alert_email(duration_minutes, FORCE_EMAIL)

        if should_email:
            print(f"📧 Sending alert email to {to_email}...")
            send_urgent_email(
                get_email_markup(),
                len(ALERTS),
                duration_str,
                tracking_info["incident_start"],
                to_email,
                incident_observations=tracking_info["failures_total"],
            )
        else:
            print(f"⏳ Skipping email — incident active for {duration_str}")

    else:
        # No alerts → incident resolved
        # This resets heartbeat incident state but does not send a recovery
        # email in the current implementation.
        tracking_info = update_incident_tracking(0)
        if tracking_info["incident_active"] is False:
            print("✅ Incident has been resolved. Tracking reset.")
        else:
            print("⚠️ ALERTS cleared, but tracking still active. This shouldn't happen.")

    tracking_info = load_tracking()
    today = utc_today_str()
    if FORCE_CERTIFICATE_CHECK or should_run_certificate_check(tracking_info, today):
        if FORCE_CERTIFICATE_CHECK:
            LOGGER.info("Forcing certificate checks despite daily gate")
        # Certificate checks are lower frequency than heartbeat checks. The
        # tracking file prevents running them more than once per UTC day.
        certificate_alerts = do_certificate_checks(websites)
        update_certificate_tracking(today, alerted=bool(certificate_alerts))

        if certificate_alerts:
            print(
                f"📧 Sending certificate alert email for {len(certificate_alerts)} issue(s)..."
            )
            send_urgent_email(
                get_certificate_email_markup(certificate_alerts),
                len(certificate_alerts),
                "0m 0s",
                now_iso(),
                PARSER.get("DEFAULT", "ALERTS_EMAIL"),
                subject="CERTIFICATE EXPIRY WARNING - PythonMonitorScript",
            )
    else:
        print(f"⏳ Skipping certificate checks — already checked on {today}")

    # Cleanup browser session
    # Always close Chrome if it was opened for screenshots.
    if SCREENSHOTS_ENABLED and BROWSER is not None:
        BROWSER.quit()

    # Delete all temporary screenshot and HTML files
    # These files are per-run diagnostic artifacts and should not accumulate.
    if SCREENSHOTS_ENABLED and not DRY_RUN and SCREENSHOTS:
        deleted = set()
        for _, filename in SCREENSHOTS:
            if filename and filename not in deleted:
                deleted.add(filename)
                try:
                    os.remove(filename)
                    print(f"🧹 Deleted: {filename}")
                except FileNotFoundError:
                    print(f"⚠️ File not found (already deleted?): {filename}")
                except Exception as e:
                    print(f"❌ Error deleting {filename}: {e}")
