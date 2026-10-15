# Toyota of Vero Beach -> Meta vehicle catalog feed

Every 3 hours GitHub Actions runs `scraper.py`, which reads every new/used vehicle on
verobeachtoyota.com (via the site's sitemap) and writes `feed/vehicles.csv` in Meta's
vehicle catalog format. Sold units drop off the site, drop out of the CSV, and Meta
removes them on its next fetch.

Feed URL for Meta:
https://raw.githubusercontent.com/<you>/<repo>/main/feed/vehicles.csv

Files written each run:
- feed/vehicles.csv   the feed
- feed/changes.log    VINs added / removed (sold) each run
- feed/problems.csv   units skipped (no price, no photos, used with no mileage)

Safety: if a run finds fewer than half of last run's vehicles, or parses none, it stops
and leaves the previous feed in place.

Settings (new/used, which price, photo count, request delay) are in the CONFIG block at
the top of scraper.py.
