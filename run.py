#!/usr/bin/env python3
import json
import requests
import configparser
import os
import time
import uuid
import asyncio
import aiohttp
import sys
from multidict import CIMultiDictProxy
from selenium import webdriver
from datetime import datetime, timezone

scriptdir = os.path.dirname(os.path.abspath(__file__))
os.chdir(scriptdir)

ALERTS = []
PARSER = configparser.ConfigParser()
BROWSER = None
SCREENSHOTS = []
SHOW_HEADERS = "--show-headers" in sys.argv
TAKE_SCREENSHOT = "--take-screenshot" in sys.argv

def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def load_tracking():
    path = os.path.join(scriptdir, "tracking.json")
    if not os.path.exists(path):
        return {
            "incident_active": False,
            "incident_start": None,
            "incident_last_seen": None,
            "incident_duration": "0s",
            "failures_total": 0,
        }

    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading tracking file: {e}")
        return {}


def save_tracking(data: dict):
    path = os.path.join(scriptdir, "tracking.json")
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error writing tracking file: {e}")


def update_incident_tracking(alert_count: int):
    """
    Adjusts incident tracking info based on number of alerts this run.
    Starts or ends an incident as needed.
    """
    tracking = load_tracking()
    now = now_iso()

    if alert_count > 0:
        if not tracking.get("incident_active"):
            print("⚠️ Incident STARTING")
            tracking["incident_active"] = True
            tracking["incident_start"] = now
            tracking["failures_total"] = alert_count
        else:
            tracking["failures_total"] += alert_count

        tracking["incident_last_seen"] = now

        # Update duration
        start_dt = datetime.fromisoformat(tracking["incident_start"]).replace(tzinfo=timezone.utc)
        duration = datetime.now(timezone.utc) - start_dt
        mins, secs = divmod(duration.total_seconds(), 60)
        tracking["incident_duration"] = f"{int(mins)}m {int(secs)}s"

        save_tracking(tracking)
        return tracking

    else:
        if tracking.get("incident_active"):
            print("✅ Incident CLEARED")

        # Reset tracking
        tracking = {
            "incident_active": False,
            "incident_start": None,
            "incident_last_seen": None,
            "incident_duration": "0s",
            "failures_total": 0,
        }
        save_tracking(tracking)
        return tracking

def get_website_dictionary():
    sites_config_file = open(os.path.join(scriptdir, "sites.json"))
    sites_to_monitor = json.load(sites_config_file)
    return sites_to_monitor


def take_endpoint_screenshot(nonce=str, endpoint=str):
    path = PARSER.get("DEFAULT", "TMP_PATH_SCREENSHOTS")

    # Filename based on path and nonce
    filename = f"{path}/screenshot_{nonce}.png"

    try:
        BROWSER.get(endpoint)
        time.sleep(2)  # Adjust the sleep duration based on your requirements

        BROWSER.save_screenshot(filename)
        SCREENSHOTS.append((nonce, filename))
    except Exception as e:
        print(f"Error accessing {endpoint}: {e}")
        BROWSER.save_screenshot(filename)
        SCREENSHOTS.append((nonce, filename))
        # TODO: Handle the error as needed, e.g., log it or take alternative action


async def do_endpoint_check(sites, site, endpoint):
    print(
        "- Checking endpoint "
        + str(endpoint)
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
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://" + str(site) + str(endpoint), timeout=timeout, headers=headers
            ) as response:
                response_body = await response.text()
                response_headers = response.headers
                expected_status = int(sites["sites"][site]["endpoints"][endpoint]["status"])
                alert_raised = False

                if response.status != expected_status:
                    status_nonce = str(uuid.uuid4().int)[:16]
                    ALERTS.append({
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
                    })
                    alert_raised = True

                    if TAKE_SCREENSHOT and SCREENSHOTS_ENABLED:
                        take_endpoint_screenshot(status_nonce, f"https://{site}{endpoint}")
                    html_path = save_html_to_file(status_nonce, response_body)
                    if html_path:
                        SCREENSHOTS.append((status_nonce, html_path))

                search_key = sites["sites"][site]["endpoints"][endpoint]["dom_contains"]
                if search_key and search_key not in response_body:
                    dom_nonce = str(uuid.uuid4().int)[:16]
                    ALERTS.append({
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
                    })
                    alert_raised = True

                    if TAKE_SCREENSHOT:
                        take_endpoint_screenshot(dom_nonce, f"https://{site}{endpoint}")
                    html_path = save_html_to_file(dom_nonce, response_body)
                    if html_path:
                        SCREENSHOTS.append((dom_nonce, html_path))

                if alert_raised:
                    print(f"❌ Alert raised for {site}{endpoint} - Exception: {ALERTS[-1]['alert']['exception']}")
                else:
                    print(f"   ✅ Passed: {endpoint}")

    except Exception as ex:
        message = str(ex) or "Unreachable, response code is 0"
        print("endpoint seems to be unreachable, response code is 0")
        print("exception: " + message)

        fallback_nonce = str(uuid.uuid4().int)[:16]
        ALERTS.append({
            "alert": {
                "site": site,
                "endpoint": endpoint,
                "expected": int(sites["sites"][site]["endpoints"][endpoint]["status"]),
                "received": 0,
                "exception": message,
                "nonce": fallback_nonce,
                "body": None,
                "headers": None,
            }
        })

        if TAKE_SCREENSHOT:
            take_endpoint_screenshot(fallback_nonce, f"https://{site}{endpoint}")



def do_heartbeat_check(sites):
    print("do_heartbeat_check started")
    loop = asyncio.get_event_loop()
    for site in sites["sites"]:
        should_check = sites["sites"][site]["check"]
        if should_check:
            print("    ")
            print("Starting checks for " + site)
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

def save_html_to_file(nonce: str, html: str) -> str:
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

def send_urgent_email(
    html_body,
    failure_count=0,
    incident_start_timestamp_delta=str,
    incident_start_timestamp=str,
    to_address=str
):
    print("send_urgent_email started")
    print("pulling email template")
    html_template = open(os.path.join(scriptdir, "email-content.html"))
    html_template = html_template.read()
    print("replacing variables in the template")
    html_template = str(html_template).replace("{{replace_alerts}}", html_body)
    html_template = str(html_template).replace("{{failure_count}}", str(failure_count))
    html_template = str(html_template).replace(
        "{{incident_start_timestamp_delta}}", str(incident_start_timestamp_delta)
    )
    html_template = str(html_template).replace(
        "{{incident_start_timestamp_pretty}}",
        str(incident_start_timestamp),
    )

    files = []
    added_filenames = set()

    for nonce, filename in SCREENSHOTS:
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
            print(f"File not found: {filename}")
        except Exception as e:
            print(f"Error reading {filename}: {e}")

    print(f"posting request to mailgun... to {to_address}")
    email_post = requests.post(
        f"https://api.mailgun.net/v3/{PARSER.get('DEFAULT', 'MAILGUN_DOMAIN')}/messages",
        auth=("api", PARSER.get("DEFAULT", "MAILGUN_PRIVATE_KEY")),
        files=files,
        data={
            "from": PARSER.get("DEFAULT", "MAILGUN_FROM"),
            "to": [to_address],
            "subject": "URGENT NOTIFICATION - PythonMonitorScript",
            "html": html_template,
        },
    )
    print(str(email_post.status_code))
    print(str(email_post.text))
    print(str(email_post.headers))
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
    print("Reading data from config.ini")
    PARSER.read("config.ini")
    SCREENSHOTS_ENABLED = PARSER.getboolean("DEFAULT", "SCREENSHOTS_ENABLED")

    # Set up browser headless options
    options = webdriver.ChromeOptions()
    if not PARSER.getboolean("DEFAULT", "DEBUG"):
        options.add_argument("--headless")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    # Launch browser if screenshot capture is enabled
    if SCREENSHOTS_ENABLED:
        BROWSER = webdriver.Chrome(options=options)

    # Run endpoint checks
    do_heartbeat_check(get_website_dictionary())

    # === POST-CHECK HANDLING ===
    if ALERTS:
        print(f"🔥 ALERTS detected: {len(ALERTS)} issue(s) found")
        tracking_info = update_incident_tracking(len(ALERTS))
        duration_str = tracking_info["incident_duration"]
        duration_minutes = int(duration_str.split("m")[0])
        to_email = PARSER.get("DEFAULT", "ALERTS_EMAIL")

        if duration_minutes >= 300:
            print("🚨 Escalation threshold reached (5+ hours). Switching to escalation email.")
            to_email = PARSER.get("DEFAULT", "ESCALATION_EMAIL")

        # Email schedule rules
        should_email = (
            5 <= duration_minutes < 30
            or (duration_minutes >= 30 and duration_minutes % 15 == 0)
        )

        if should_email:
            print(f"📧 Sending alert email to {to_email}...")
            send_urgent_email(
                get_email_markup(),
                tracking_info["failures_total"],
                duration_str,
                tracking_info["incident_start"],
                to_email,
            )
        else:
            print(f"⏳ Skipping email — incident active for {duration_str}")

    else:
        # No alerts → incident resolved
        tracking_info = update_incident_tracking(0)
        if tracking_info["incident_active"] is False:
            print("✅ Incident has been resolved. Tracking reset.")
        else:
            print("⚠️ ALERTS cleared, but tracking still active. This shouldn't happen.")

    # Cleanup browser session
    if SCREENSHOTS_ENABLED:
        BROWSER.quit()

    # Delete all temporary screenshot and HTML files
    if SCREENSHOTS_ENABLED and SCREENSHOTS:
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
