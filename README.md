# PythonMonitorScript
Monitor websites using a JSON object and a cronjob
This was created so I could make sure certain websites are online and reactively resolve issues as they arise.

I have this running in a Linux server running Ubuntu, on a cronjob which runs every 1 minute. The endpoint scanning is asynchronous to complete as fast as possible.

### Usage
Run the monitor normally:

```bash
python run.py
```

Show the built-in manual:

```bash
python run.py --manual
```

Useful testing flags:

- `--dry-run` runs checks without writing `tracking.json`, creating response artifacts, or sending Mailgun email.
- `--force-email` sends an alert to `ALERTS_EMAIL` whenever current heartbeat failures exist, bypassing the normal email cadence and escalation routing.
- `--force-certificate-check` runs certificate checks even if they already ran today.
- `--show-headers` includes response headers and body content in heartbeat alert emails.
- `--take-screenshot` captures Selenium screenshots for failed checks when screenshots are enabled in `config.ini`.
- `--log-level DEBUG|INFO|WARNING|ERROR` controls console log verbosity.

### Email escalation
Heartbeat alert emails go to `ALERTS_EMAIL` first. If the same incident remains unresolved for `ESCALATION_AFTER_MINUTES`, future heartbeat alert emails go to `ESCALATION_EMAIL`.

`ESCALATION_AFTER_MINUTES` is optional in `config.ini` and defaults to `300` minutes.

Manual test sends with `--force-email` always go to `ALERTS_EMAIL` so escalation recipients are not notified during testing.

### Certificate monitoring
The script also checks HTTPS certificate expiration for every domain in `sites.json` where `check` is set to `true` and `scheme` is `https`.

Because the cronjob runs every minute, certificate checks are gated by `tracking.json` and only run once per UTC day. If a certificate expires in 10 days or less, or if the certificate cannot be fetched or parsed, the script sends a Mailgun alert email using the same email template flow as heartbeat alerts.

Certificate state is tracked with `certificate_last_checked_date` and `certificate_last_alerted_date` in `tracking.json`.

### HTTP endpoints
Sites default to HTTPS. To monitor a local HTTP-only server, set `scheme` to `http` on that site in `sites.json`:

```json
{
  "sites": {
    "localhost:8000": {
      "check": true,
      "scheme": "http",
      "endpoints": {
        "/": {
          "status": 200,
          "dom_contains": ""
        }
      }
    }
  }
}
```

HTTP sites are skipped by certificate monitoring.

### Selenium
If enabled, when an error is detected, Selenium kicks in to take a screenshot of the website from the Chrome driver.

### Time
We are using ISO 8601 for time management
