# PythonMonitorScript
Monitor websites using a JSON object and a cronjob
This was created so I could make sure certain websites are online and reactively resolve issues as they arise.

I have this running in a Linux server running Ubuntu, on a cronjob which runs every 1 minute. The endpoint scanning is asynchronous to complete as fast as possible.

When an error is detected, Selenium kicks in to take a screenshot of the website from the Chrome driver.