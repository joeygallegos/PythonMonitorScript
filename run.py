#!/usr/bin/env python3
import json
import requests
import configparser
import os
import time
import uuid
import asyncio
import aiohttp

from datetime import datetime
from selenium import webdriver

scriptdir = os.path.dirname(os.path.abspath(__file__))
os.chdir(scriptdir)

ALERTS = []
PARSER = configparser.ConfigParser()
BROWSER = None
SCREENSHOTS = []


def get_website_dictionary():
    sites_config_file = open(os.path.join(scriptdir, "sites.json"))
    sites_to_monitor = json.load(sites_config_file)
    return sites_to_monitor


def take_screenshot(nonce=str, endpoint=str):
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
        "checking endpoint "
        + str(endpoint)
        + " for a status code "
        + str(sites["sites"][site]["endpoints"][endpoint]["status"])
    )
    # Setup the nonce for this test
    nonce = str(uuid.uuid4().int)[:16]
    try:
        # set timeout for whole request
        timeout = aiohttp.ClientTimeout(total=5)

        # specify the User-Agent header
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 PythonMonitorScript/1.0",
            "Accept": "*/*",
            "Connection": "close",
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://" + str(site) + str(endpoint), timeout=timeout, headers=headers
            ) as response:
                # Extract the response body as a string
                response_body = await response.text()

                if response.status != int(
                    sites["sites"][site]["endpoints"][endpoint]["status"]
                ):
                    print(
                        "response code not "
                        + str(sites["sites"][site]["endpoints"][endpoint]["status"])
                        + ".. received "
                        + str(response.status)
                    )
                    ALERTS.append(
                        {
                            "alert": {
                                "site": site,
                                "endpoint": endpoint,
                                "expected": int(
                                    sites["sites"][site]["endpoints"][endpoint][
                                        "status"
                                    ]
                                ),
                                "received": response.status,
                                "exception": "Status code mismatch",
                                "nonce": nonce,
                            }
                        }
                    )
                    take_screenshot(nonce, "https://" + str(site) + str(endpoint))
                # if sites config has "dom_contains" string value, then check if that string is in the response text
                search_key = sites["sites"][site]["endpoints"][endpoint]["dom_contains"]

                # If search key is not empty, then search using it
                if search_key:
                    if str(response_body).find(str(search_key)) == -1:
                        print(
                            "response text does not contain "
                            + str(
                                sites["sites"][site]["endpoints"][endpoint][
                                    "dom_contains"
                                ]
                            )
                        )
                        ALERTS.append(
                            {
                                "alert": {
                                    "site": site,
                                    "endpoint": endpoint,
                                    "expected": 0,
                                    "received": 0,
                                    "exception": "DOM string mismatch",
                                    "nonce": nonce,
                                }
                            }
                        )
                        take_screenshot(nonce, "https://" + str(site) + str(endpoint))
    except Exception as ex:
        message = str(ex)
        if not message:
            message = "Unreachable, response code is 0"
            print("endpoint seems to be unreachable, response code is 0")
            print("exception: " + str(message))
            ALERTS.append(
                {
                    "alert": {
                        "site": site,
                        "endpoint": endpoint,
                        "expected": int(
                            sites["sites"][site]["endpoints"][endpoint]["status"]
                        ),
                        "received": 0,
                        "exception": ex,
                        "nonce": nonce,
                    }
                }
            )
            take_screenshot(nonce, "https://" + str(site) + str(endpoint))


def do_heartbeat_check(sites):
    print("do_heartbeat_check started")
    loop = asyncio.get_event_loop()
    for site in sites["sites"]:
        should_check = sites["sites"][site]["check"]
        if should_check:
            print("----")
            print("starting heartbeat check for " + site)
            for endpoint in sites["sites"][site]["endpoints"]:
                loop.run_until_complete(do_endpoint_check(sites, site, endpoint))
        else:
            print("check variable set to false for " + site)

    print("do_heartbeat_check ended")


def get_email_markup():
    print("get_email_markup started")

    html_body = ""
    for alert in ALERTS:
        html_body += "<strong>Site:</strong> " + str(alert["alert"]["site"]) + " <br>"
        html_body += (
            "<strong>Endpoint:</strong> " + str(alert["alert"]["endpoint"]) + " <br>"
        )
        html_body += (
            "<strong>Response Code:</strong> "
            + str(alert["alert"]["received"])
            + " <br>"
        )

        if alert["alert"]["exception"]:
            html_body += (
                "<strong>Exception:</strong> "
                + str(alert["alert"]["exception"])
                + " <br>"
            )

        # Debug nonce
        html_body += "<strong>Nonce:</strong> " + str(alert["alert"]["nonce"]) + " <br>"

        # Inline pictures from Selenium
        html_body += (
            "<strong>Screenshot:</strong><br><img src='cid:"
            + str(alert["alert"]["nonce"])
            + ".png' alt='Nonce Image'>"
        )
        html_body += "<br>"
    return html_body


def send_urgent_email(
    html_body,
    failure_count=0,
    incident_start_timestamp_delta=str,
    incident_start_timestamp=str,
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
    for nonce, filename in SCREENSHOTS:
        try:
            with open(filename, "rb") as screenshot_file:
                file_content = screenshot_file.read()
                files.append(("inline", (f"{nonce}.png", file_content)))
        except FileNotFoundError:
            print(f"File not found: {filename}")
        except Exception as e:
            print(f"Error reading {filename}: {e}")

    print("posting request to mailgun")
    return requests.post(
        "https://api.mailgun.net/v3/"
        + PARSER.get("DEFAULT", "MAILGUN_DOMAIN")
        + "/messages",
        auth=("api", PARSER.get("DEFAULT", "MAILGUN_PRIVATE_KEY")),
        files=files,
        data={
            "from": PARSER.get("DEFAULT", "MAILGUN_FROM"),
            "to": [PARSER.get("DEFAULT", "ALERTS_EMAIL")],
            "subject": "URGENT NOTIFICATION - PythonMonitorScript",
            "html": html_template,
        },
    )


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

    # Set up options for browser headless mode
    options = webdriver.ChromeOptions()

    is_debug = PARSER.getboolean("DEFAULT", "DEBUG")
    if is_debug is False:
        options.add_argument("--headless")

    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    # Initialize the WebDriver with the options
    BROWSER = webdriver.Chrome(options=options)

    # trigger checks for each site and associated endpoints
    do_heartbeat_check(get_website_dictionary())

    # check if global alerts array list has values
    if ALERTS:
        current_fail_ticks = get_failed_ticks()
        next_fail_ticks = int(current_fail_ticks) + 1
        set_failed_ticks(next_fail_ticks)

        # if we detect our first fail, then set the incident start time
        print("Checking if we need to set the incident start time")
        if next_fail_ticks >= 1 and get_incident_start_timestamp() == "None":
            print("Setting the incident start time since >= 1")
            set_incident_start_timestamp(str(datetime.now()))

        # if 5 fails back to back, then clearly it's an issue
        if next_fail_ticks >= 5 and next_fail_ticks < 30:
            print("Sending an alert to emails")
            send_urgent_email(
                get_email_markup(),
                next_fail_ticks,
                get_pretty_time(datetime.fromisoformat(get_incident_start_timestamp())),
                get_incident_start_timestamp(),
            )

        # if more than 30 fails back to back, then only alert once every 15 failed ticks
        if next_fail_ticks >= 30 and next_fail_ticks % 15 == 0:
            print("Sending an alert to emails")
            send_urgent_email(
                get_email_markup(),
                next_fail_ticks,
                get_pretty_time(datetime.fromisoformat(get_incident_start_timestamp())),
                get_incident_start_timestamp(),
            )

    else:
        count_fails = get_failed_ticks()

        # if the issue is resolved, but count is something very high
        # then lets reset the tracking ticks to 5
        # and for each time theres no alerts, lets subtract 1 until that number is 0
        print("Failure counter is currently at " + str(count_fails))
        if count_fails > 5:
            set_failed_ticks(5)
            print("Incident seems to be resolved...")
            print(" - Resetting counter to 5")
            print(" - Incrementally decreasing the failure count down to 0")

        # if there are no alerts but count is still positive value, then decrease count by 1
        elif count_fails > 0:
            set_failed_ticks(int(count_fails) - 1)
            print("decreasing failure count")

        # if fail ticks resets to 0, then clear the incident start date
        if count_fails == 0:
            set_incident_start_timestamp(None)
            print("All clear")

    # Cleanup the running browser
    BROWSER.quit()

    # Cleanup delete the screenshot files
    if SCREENSHOTS:
        for nonce, filename in SCREENSHOTS:
            try:
                os.remove(filename)
                print(f"Deleted: {filename}")
            except FileNotFoundError:
                print(f"File not found: {filename}")
            except Exception as e:
                print(f"Error deleting {filename}: {e}")
