# PythonMonitorScript
Monitor websites using a JSON object and a cronjob
This was created so I could make sure certain websites are online and reactively resolve issues as they arise.

I have this running in a Linux server running Ubuntu, on a cronjob which runs every 1 minute. The endpoint scanning is asynchronous to complete as fast as possible.

### Certificate monitoring
The script also checks HTTPS certificate expiration for every domain in `sites.json` where `check` is set to `true`.

Because the cronjob runs every minute, certificate checks are gated by `tracking.json` and only run once per UTC day. If a certificate expires in 10 days or less, or if the certificate cannot be fetched or parsed, the script sends a Mailgun alert email using the same email template flow as heartbeat alerts.

Certificate state is tracked with `certificate_last_checked_date` and `certificate_last_alerted_date` in `tracking.json`.

### Selenium
If enabled, when an error is detected, Selenium kicks in to take a screenshot of the website from the Chrome driver.

### Time
We are using ISO 8601 for time management
