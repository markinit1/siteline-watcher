"""
Siteline watcher.

Runs on a schedule in GitHub Actions. For every active watch in Siteline, it asks
ReserveCalifornia or Recreation.gov (through Camply) which campsites are open,
and pushes an alert to your phone when one of your sites newly opens up.
"""
import base64
import datetime as dt
import json
import logging
import os
import re
import sys
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import credentials, firestore, messaging
from google.cloud.firestore_v1.base_query import FieldFilter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("siteline")
for noisy in ("camply", "urllib3", "google"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

PACIFIC = ZoneInfo("America/Los_Angeles")
APP_URL = os.environ.get("SITELINE_URL", "https://siteline-app.netlify.app/").rstrip("/") + "/"
MAX_OPEN = 25


def today() -> dt.date:
    return dt.datetime.now(PACIFIC).date()


def short_date(d: dt.date) -> str:
    return d.strftime("%a, %b ") + str(d.day)


def init_firebase():
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_B64", "").strip()
    if not raw:
        sys.exit("The FIREBASE_SERVICE_ACCOUNT_B64 secret is missing from this repo.")
    info = json.loads(base64.b64decode(raw))
    firebase_admin.initialize_app(credentials.Certificate(info))
    return firestore.client()


# ---------- Site numbers ----------
def norm_site(value) -> str:
    s = str(value or "").lower()
    s = re.sub(r"\b(site|campsite|space|spot)\b|#|\s", "", s)
    return re.sub(r"(?<!\d)0+(?=\d)", "", s)  # "022" -> "22"


def site_matches(campsite_name: str, wanted: list) -> bool:
    if not wanted:
        return True
    n = norm_site(campsite_name)
    nums = re.findall(r"\d+", n)
    last = (nums[-1].lstrip("0") or "0") if nums else ""
    for w in wanted:
        if n == w or (w.isdigit() and last == w):
            return True
    return False


def display_site(campsite_name: str) -> str:
    n = str(campsite_name or "").strip()
    m = re.fullmatch(r"(?i)(?:site|campsite)?\s*#?\s*0*(\w+)", n)
    return m.group(1) if m else n


# ---------- Finding the campground on the reservation system ----------
def facility_for(cg_ref, cg: dict):
    """Returns (facility_id, problem). Exactly one of them is set."""
    if cg.get("providerFacilityId"):
        return str(cg["providerFacilityId"]).strip(), None
    system = cg.get("system")
    url = cg.get("bookingUrl") or ""

    if system == "rgov":
        m = re.search(r"/campgrounds/(\d+)", url)
        if m:
            cg_ref.update({"providerFacilityId": m.group(1)})
            return m.group(1), None
        return None, "Add this campground's Recreation.gov link (the number in it is the campground ID)."

    if system == "rc":
        if cg.get("providerMatches"):
            return None, "Pick which campground to watch on the campground's page."
        return resolve_reservecalifornia(cg_ref, cg)

    return None, "Siteline can watch ReserveCalifornia and Recreation.gov campgrounds."


def resolve_reservecalifornia(cg_ref, cg: dict):
    from camply.providers import ReserveCalifornia

    rc = ReserveCalifornia()
    for term in [cg.get("name"), cg.get("shortName")]:
        if not term:
            continue
        try:
            found = rc.find_campgrounds(search_string=term, verbose=False) or []
        except SystemExit:
            found = []
        except Exception as exc:  # noqa: BLE001
            log.warning("Campground lookup for %r failed: %s", term, exc)
            return None, f"Could not reach ReserveCalifornia to look up the campground ({short_error(exc)})."
        if not found:
            continue
        matches = [
            {"id": str(f.facility_id), "name": f.facility_name, "park": f.recreation_area or ""}
            for f in found
        ][:12]
        if len(matches) == 1:
            cg_ref.update({
                "providerFacilityId": matches[0]["id"],
                "providerFacilityName": matches[0]["name"],
            })
            return matches[0]["id"], None
        cg_ref.update({"providerMatches": matches})
        return None, "Pick which campground to watch on the campground's page."
    return None, f'ReserveCalifornia has no campground matching "{cg.get("name")}". Add its ID on the campground page.'


# ---------- Searching ----------
def run_search(cg: dict, facility_id: str, watch: dict, rig_length):
    from camply.containers import SearchWindow

    start = max(dt.date.fromisoformat(watch["searchStart"]), today())
    end = dt.date.fromisoformat(watch["searchEnd"])
    nights = max(1, int(watch.get("nights") or 1))
    if (end - start).days < nights:
        return None  # the dates have passed

    window = SearchWindow(start_date=start, end_date=end)
    weekends = bool(watch.get("weekendsOnly"))
    try:
        if cg.get("system") == "rgov":
            from camply.search import SearchRecreationDotGov

            extra = {}
            if not watch.get("siteNumbers") and rig_length:
                extra["equipment"] = [("Trailer", int(rig_length))]
            search = SearchRecreationDotGov(
                search_window=window, campgrounds=[int(facility_id)],
                nights=nights, weekends_only=weekends, **extra,
            )
        else:
            from camply.search import SearchReserveCalifornia

            search = SearchReserveCalifornia(
                search_window=window, recreation_area=[], campgrounds=[int(facility_id)],
                nights=nights, weekends_only=weekends,
            )
        return search.get_matching_campsites(log=False, verbose=False, continuous=False) or []
    except SystemExit as exc:
        raise RuntimeError("the reservation system did not recognize this campground ID") from exc


def short_error(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    if "403" in text:
        return "the reservation site refused the request (403)"
    if "429" in text:
        return "the reservation site asked us to slow down (429)"
    return text.splitlines()[0][:160]


# ---------- Alerts ----------
def send_push(uref, devices, title: str, body: str, url: str, tag: str) -> int:
    targets = [(d.id, (d.to_dict() or {}).get("token")) for d in devices]
    targets = [(doc_id, token) for doc_id, token in targets if token]
    if not targets:
        return 0
    msgs = [
        messaging.Message(
            token=token,
            data={"title": title, "body": body, "url": url, "tag": tag},
            webpush=messaging.WebpushConfig(headers={"Urgency": "high", "TTL": "1800"}),
        )
        for _, token in targets
    ]
    result = messaging.send_each(msgs)
    for (doc_id, _), r in zip(targets, result.responses):
        if r.success:
            continue
        if isinstance(r.exception, (messaging.UnregisteredError, messaging.SenderIdMismatchError)):
            uref.collection("devices").document(doc_id).delete()
            log.info("Removed a phone that no longer accepts alerts")
        else:
            log.warning("Alert failed: %s", r.exception)
    return result.success_count


def process_watch(uref, wdoc, devices, rig_length) -> int:
    w = wdoc.to_dict() or {}
    wref = wdoc.reference
    cg_ref = uref.collection("campgrounds").document(w.get("campgroundId") or "-")
    cg_snap = cg_ref.get()
    if not cg_snap.exists:
        wref.update({"active": False, "status": "error", "statusText": "This campground was deleted.",
                     "lastChecked": firestore.SERVER_TIMESTAMP})
        return 0
    cg = cg_snap.to_dict() or {}
    label = cg.get("shortName") or cg.get("name") or "Campground"

    facility_id, problem = facility_for(cg_ref, cg)
    if problem:
        wref.update({"status": "needs-campground", "statusText": problem, "lastChecked": firestore.SERVER_TIMESTAMP})
        return 0

    try:
        found = run_search(cg, facility_id, w, rig_length)
    except Exception as exc:  # noqa: BLE001
        log.warning("Search failed for %s: %s", label, exc)
        wref.update({"status": "error", "statusText": "Last check failed: " + short_error(exc) + ".",
                     "lastChecked": firestore.SERVER_TIMESTAMP})
        return 0

    if found is None:
        wref.update({"active": False, "status": "expired", "statusText": "These dates have passed.",
                     "openNow": [], "openKeys": [], "lastChecked": firestore.SERVER_TIMESTAMP})
        return 0

    wanted = [norm_site(s) for s in (w.get("siteNumbers") or []) if str(s).strip()]
    open_now = {}
    for c in found:
        if not site_matches(c.campsite_site_name, wanted):
            continue
        arrive = c.booking_date.date()
        leave = c.booking_end_date.date()
        key = f"{norm_site(c.campsite_site_name)}|{arrive.isoformat()}|{leave.isoformat()}"
        open_now[key] = {
            "site": display_site(c.campsite_site_name),
            "loop": c.campsite_loop_name or "",
            "arrive": arrive.isoformat(),
            "leave": leave.isoformat(),
            "nights": int(c.booking_nights or (leave - arrive).days),
            "url": c.booking_url or cg.get("bookingUrl") or "",
        }

    previous = set(w.get("openKeys") or [])
    new_keys = [k for k in open_now if k not in previous]
    ordered = sorted(open_now.values(), key=lambda x: (x["arrive"], x["site"]))[:MAX_OPEN]
    wref.update({
        "status": "watching",
        "statusText": "",
        "openNow": ordered,
        "openKeys": sorted(open_now.keys()),
        "lastChecked": firestore.SERVER_TIMESTAMP,
    })
    log.info("%s: %d open, %d new", label, len(open_now), len(new_keys))

    if not new_keys:
        return 0
    first = open_now[new_keys[0]]
    a, b = dt.date.fromisoformat(first["arrive"]), dt.date.fromisoformat(first["leave"])
    if len(new_keys) == 1:
        title = f"{label}: site {first['site']} opened up"
        body = f"{short_date(a)} to {short_date(b)}. Tap to book."
    else:
        sites = ", ".join(sorted({open_now[k]["site"] for k in new_keys}))[:80]
        title = f"{label}: {len(new_keys)} openings"
        body = f"Sites {sites}, starting {short_date(a)}. Tap to book."
    send_push(uref, devices, title, body, APP_URL + "#/watching", "watch-" + wdoc.id)
    return len(new_keys)


def main():
    db = init_firebase()
    test_push = os.environ.get("TEST_PUSH", "").lower() == "true"
    watches_checked = openings = 0

    for user in db.collection("users").stream():
        uref = user.reference
        rig_length = ((user.to_dict() or {}).get("settings") or {}).get("rigLength")
        devices = list(uref.collection("devices").stream())

        if test_push:
            sent = send_push(uref, devices, "Siteline alerts are working",
                             "This test came from your watcher on GitHub.", APP_URL + "#/watching", "test")
            log.info("Test alert sent to %d phone(s)", sent)

        active = uref.collection("watches").where(filter=FieldFilter("active", "==", True)).stream()
        for wdoc in active:
            watches_checked += 1
            try:
                openings += process_watch(uref, wdoc, devices, rig_length)
            except Exception:  # noqa: BLE001 - one bad watch should never stop the others
                log.exception("Watch %s failed", wdoc.id)

    log.info("Checked %d watch(es), %d new opening(s)", watches_checked, openings)


if __name__ == "__main__":
    main()
