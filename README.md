# Siteline watcher

Checks campsite availability for the watches you set up in Siteline and sends
an alert to your phone when one of your sites opens up. Runs on GitHub Actions
about every 10 minutes.

Secret needed: `FIREBASE_SERVICE_ACCOUNT_B64` (the Firebase service account
JSON for `siteline-app`, base64-encoded).

To send a test alert: Actions tab, "Watch for cancellations", Run workflow,
check "Send a test alert", Run.
