#!/usr/bin/env python3
import json
import requests
import configparser
import os

import asyncio
import aiohttp

from datetime import datetime

scriptdir = os.path.dirname(os.path.abspath(__file__))
os.chdir(scriptdir)

ALERTS = []
PARSER = configparser.ConfigParser()


def get_website_dictionary():
    sites_config_file = open(os.path.join(scriptdir, "sites.json"))
    sites_to_monitor = json.load(sites_config_file)
    return sites_to_monitor


async def do_endpoint_check(sites, site, endpoint):
    print(
        "checking endpoint "
        + str(endpoint)
        + " for a status code "
        + str(sites["sites"][site]["endpoints"][endpoint]["status"])
    )
    try:
        # set timeout for whole request
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://" + str(site) + str(endpoint), timeout=timeout
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
                            }
                        }
                    )
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
                                }
                            }
                        )
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
                }
            }
        )


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
    print("posting request to mailgun")
    return requests.post(
        "https://api.mailgun.net/v3/"
        + PARSER.get("DEFAULT", "MAILGUN_DOMAIN")
        + "/messages",
        auth=("api", PARSER.get("DEFAULT", "MAILGUN_PRIVATE_KEY")),
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
        print("opening tracking.json with w+")
        with open(os.path.join(scriptdir, "tracking.json"), "w+") as tracking_file:
            # // json.dumps() returns a string and not a json object
            print("write tracking.json")
            tracking_file.write(json.dumps(new_data))
            print("close tracking.json")
            tracking_file.close()
    except Exception as ex:
        print("write_data_to_manifest exception: " + str(ex))


# get failed ticks from file storage
def get_failed_ticks():
    with open(os.path.join(scriptdir, "tracking.json")) as tracking_file:
        current_json_tracking = json.loads(tracking_file.read())
        tracking_file.close()
    return int(current_json_tracking.get("failed_count"))


# manipulate manifest array and trigger a write to the manifest file
def set_failed_ticks(count=0):
    print("get object data from read_data_from_manifest()")
    tmp_data = read_data_from_manifest()

    print("replace entry with new count")
    tmp_data["failed_count"] = count

    write_data_to_manifest(tmp_data)


# manipulate manifest array and trigger a write to the manifest file
def set_incident_start_timestamp(new_timestamp=str):
    print("trying to set incident start timestamp to: " + str(datetime.now()))
    tmp_data = read_data_from_manifest()

    print("replace entry with new timestamp")

    # NOTE!!! can be null also
    tmp_data["incident_start_timestamp"] = new_timestamp

    write_data_to_manifest(tmp_data)


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
    print("Pulling data from config.ini")
    PARSER.read("config.ini")

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
        print("failure counter is currently at " + str(count_fails))
        if count_fails > 5:
            set_failed_ticks(5)
            print("issue seems to be resolved, resetting counter to 5")
            print("script will now incrementally decrease the failure count down to 0")

        # if there are no alerts but count is still positive value, then decrease count by 1
        elif count_fails > 0:
            set_failed_ticks(int(count_fails) - 1)
            print("decreasing failure count")

        # if fail ticks resets to 0, then clear the incident start date
        if count_fails == 0:
            set_incident_start_timestamp(None)
            print("All clear")
