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


def check_for_alerts():
    print("check_for_alerts started")
    if ALERTS:
        html_body = ""
        print("alerts exist, forming html next")
        for alert in ALERTS:
            html_body += (
                "<strong>Site:</strong> " + str(alert["alert"]["site"]) + " <br>"
            )
            html_body += (
                "<strong>Endpoint:</strong> "
                + str(alert["alert"]["endpoint"])
                + " <br>"
            )
            html_body += (
                "<strong>Response Code:</strong> "
                + str(alert["alert"]["received"])
                + " <br>"
            )
            html_body += "<br>"
        print("send_urgent_email called")
        print(send_urgent_email(html_body))


def send_urgent_email(html_body):
    print("send_urgent_email started")
    print("pulling email template")
    html_template = open(os.path.join(scriptdir, "email-content.html"))
    html_template = html_template.read()
    print("replacing variables in the template")
    html_template = str(html_template).replace("{{replace_alerts}}", html_body)
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


if __name__ == "__main__":
    print("Pulling data from config.ini")
    PARSER.read("config.ini")

    print("JSON config:")
    print(get_website_dictionary())

    do_heartbeat_check(get_website_dictionary())
    check_for_alerts()
