#!/usr/bin/env python3
import json
import requests
import configparser
import os

scriptdir = os.path.dirname(os.path.abspath(__file__))
os.chdir(scriptdir)

ALERTS = []
PARSER = configparser.ConfigParser()


def get_website_dictionary():
    sites_config_file = open(os.path.join(scriptdir, "sites.json"))
    sites_to_monitor = json.load(sites_config_file)
    return sites_to_monitor


def do_heartbeat_check(sites):
    print("do_heartbeat_check started")
    for site in sites["sites"]:
        print("----")
        print("starting heartbeat check for " + site)
        for check in sites["sites"][site]:
            print(
                "checking endpoint "
                + str(check)
                + " for a status code "
                + str(sites["sites"][site][check])
            )
            try:
                r = requests.get("https://" + str(site) + str(check))
            except (Exception):
                print("endpoint seems to be unreachable, response code is 0")
                ALERTS.append(
                    {
                        "alert": {
                            "site": site,
                            "endpoint": check,
                            "expected": int(sites["sites"][site][check]),
                            "received": 0,
                        }
                    }
                )
            else:
                if r.status_code != int(sites["sites"][site][check]):
                    print(
                        "response code not "
                        + str(sites["sites"][site][check])
                        + ".. received "
                        + str(r.status_code)
                    )
                    ALERTS.append(
                        {
                            "alert": {
                                "site": site,
                                "endpoint": check,
                                "expected": int(sites["sites"][site][check]),
                                "received": r.status_code,
                            }
                        }
                    )
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
        html_body += "<br>"
    return html_body


def send_urgent_email(html_body, failure_count=0):
    print("send_urgent_email started")
    print("pulling email template")
    html_template = open(os.path.join(scriptdir, "email-content.html"))
    html_template = html_template.read()
    print("replacing variables in the template")
    html_template = str(html_template).replace("{{replace_alerts}}", html_body)
    html_template = str(html_template).replace("{{failure_count}}", str(failure_count))
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


def get_failed_count():
    with open(os.path.join(scriptdir, "tracking.json")) as tracking_file:
        current_json_tracking = json.loads(tracking_file.read())
        tracking_file.close()
    return int(current_json_tracking.get("failed_count"))


def set_failed_count(count=0):
    with open(os.path.join(scriptdir, "tracking.json"), "w+") as tracking_file:
        tracking_file.write(json.dumps({"failed_count": count}))
        tracking_file.close()


if __name__ == "__main__":
    print("Pulling data from config.ini")
    PARSER.read("config.ini")

    print("JSON config:")
    print(get_website_dictionary())

    do_heartbeat_check(get_website_dictionary())
    if ALERTS:
        print("get_email_markup called")

        # if 5 fails back to back, then clearly it's an issue
        print("get_failed_count called")
        count_fails = get_failed_count()
        count_fails = int(count_fails) + 1
        set_failed_count(count_fails)
        markup = get_email_markup()
        if count_fails >= 5:
            print("send_urgent_email called")
            send_urgent_email(markup, count_fails)
    else:
        count_fails = get_failed_count()

        # if we resolved, but count is something high, lets reset to 5
        # and for each time theres no alerts, lets deduct that number to 0
        print("failure counter is currently at " + str(count_fails))
        if count_fails > 5:
            set_failed_count(5)
            print("seems to be fixed, resetting counter to 5")
            print("it should be decrease the failure count now")
        elif count_fails > 0:
            set_failed_count(int(count_fails) - 1)
            print("decreasing failure count")
