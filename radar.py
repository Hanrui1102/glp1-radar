"""GLP-1 Evidence Radar: source-linked metadata, never treatment recommendations."""
import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
NOTICE = "Explore published research and trial registrations. For individual treatment questions, consult a clinician."


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def get_json(base, params):
    """Serial requests, bounded retries; do not log URLs containing credentials."""
    request = Request(base + "?" + urlencode(params), headers={
        "User-Agent": "GLP1EvidenceRadar/0.1 (public research metadata)"})
    for attempt in range(3):
        time.sleep(0.4)  # below NCBI's unauthenticated 3 requests/second ceiling
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(2 ** attempt)
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def aliases(config):
    # Keep non-Latin display aliases out of English-source queries.
    return sorted({a for values in config["drugs"].values() for a in values if a.isascii()})


def classify(title, extra, config):
    text = (title + " " + extra).casefold()
    return {
        "drugs": [drug for drug, names in config["drugs"].items()
                  if any(name.casefold() in text for name in names)],
        "topics": [topic for topic, words in config["topics"].items()
                   if any(word.casefold() in text for word in words)]
    }


def pubmed(config, fetch=get_json):
    common = {"db": "pubmed", "retmode": "json", "tool": "glp1_evidence_radar"}
    if os.environ.get("NCBI_EMAIL"):
        common["email"] = os.environ["NCBI_EMAIL"]
    if os.environ.get("NCBI_API_KEY"):
        common["api_key"] = os.environ["NCBI_API_KEY"]
    query = " OR ".join('"' + a + '"[Title/Abstract]' for a in aliases(config))
    cap = config["max_records_per_source"]
    search = fetch(EUTILS + "esearch.fcgi", {
        **common, "term": query, "reldate": config["pubmed_days"],
        "datetype": "edat", "sort": "pub date", "retmax": cap})
    if "error" in search or "esearchresult" not in search:
        raise ValueError("Invalid PubMed search response")
    result = search["esearchresult"]
    if result.get("ERROR") or result.get("errorlist"):
        raise ValueError("PubMed rejected the search query")
    ids = result["idlist"]
    records = []
    for start in range(0, len(ids), 100):
        page = fetch(EUTILS + "esummary.fcgi", {**common, "id": ",".join(ids[start:start + 100])})
        for uid in ids[start:start + 100]:
            item = page["result"][uid]
            if item.get("error") or not item.get("title"):
                raise ValueError("Incomplete PubMed summary")
            title = item["title"]
            records.append({
                "id": "pubmed:" + uid, "source": "PubMed", "title": title,
                "url": "https://pubmed.ncbi.nlm.nih.gov/" + uid + "/",
                "source_date": item.get("pubdate", ""),
                "date_kind": "publication_date_as_supplied",
                "journal": item.get("fulljournalname", ""),
                "publication_types": item.get("pubtype", []),
                "evidence_kind": "Indexed publication",
                **classify(title, "", config)
            })
    return records, {"query": query, "matched": int(result["count"]),
                     "returned": len(records), "truncated": int(result["count"]) > len(ids),
                     "window": f"last {config['pubmed_days']} days by PubMed entry date"}


def normalize_trial(item, config):
    protocol = item["protocolSection"]
    identity = protocol["identificationModule"]
    status = protocol["statusModule"]
    design = protocol.get("designModule", {})
    arms = protocol.get("armsInterventionsModule", {})
    interventions = [i.get("name", "") for i in arms.get("interventions", [])]
    conditions = protocol.get("conditionsModule", {}).get("conditions", [])
    title = identity["briefTitle"]
    return {
        "id": "ctgov:" + identity["nctId"], "source": "ClinicalTrials.gov",
        "title": title, "url": "https://clinicaltrials.gov/study/" + identity["nctId"],
        "source_date": status.get("lastUpdatePostDateStruct", {}).get("date", ""),
        "date_kind": "last_update_posted", "status": status.get("overallStatus", "UNKNOWN"),
        "phases": design.get("phases", []), "study_type": design.get("studyType", "UNKNOWN"),
        "enrollment": design.get("enrollmentInfo", {}), "conditions": conditions,
        "interventions": interventions,
        "countries": sorted({loc["country"] for loc in protocol.get("contactsLocationsModule", {})
                             .get("locations", []) if loc.get("country")}),
        "has_results": item.get("hasResults", False),
        "evidence_kind": "Trial registration",
        **classify(title, " ".join(interventions), config)
    }


def trials(config, fetch=get_json):
    query = " OR ".join('"' + a + '"' for a in aliases(config))
    cap = config["max_records_per_source"]
    params = {"query.intr": query, "format": "json", "pageSize": min(cap, 100),
              "countTotal": "true", "sort": "LastUpdatePostDate:desc"}
    records, seen_tokens = [], set()
    while True:
        page = fetch("https://clinicaltrials.gov/api/v2/studies", params)
        if "studies" not in page:
            raise ValueError("Invalid ClinicalTrials.gov response")
        records.extend(normalize_trial(item, config) for item in page["studies"])
        token = page.get("nextPageToken")
        if not token or len(records) >= cap:
            break
        if token in seen_tokens:
            raise ValueError("Repeated pagination token")
        seen_tokens.add(token)
        params["pageToken"] = token
    return records[:cap], {"query": query, "matched": page.get("totalCount"),
                           "returned": min(len(records), cap),
                           "truncated": bool(token) or len(records) > cap,
                           "window": "latest updated matching registrations; no date window"}


def fingerprint(record):
    clean = {k: v for k, v in record.items() if k not in
             {"first_seen", "last_seen", "changed_at", "fingerprint"}}
    return hashlib.sha256(json.dumps(clean, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def merge(old, incoming, timestamp):
    """Missing records stay in archive; a capped result is never a deletion."""
    baseline = {r["source"] for r in old}
    merged = {record["id"]: dict(record) for record in old}
    events = []
    for record in incoming:
        record = dict(record)
        previous = merged.get(record["id"])
        digest = fingerprint(record)
        changed = previous is None or previous.get("fingerprint") != digest
        record.update(first_seen=previous["first_seen"] if previous else timestamp,
                      last_seen=timestamp, fingerprint=digest,
                      changed_at=timestamp if changed else previous["changed_at"])
        merged[record["id"]] = record
        if changed:
            summary = "Record metadata updated"
            if previous is None:
                summary = ("Initial collection" if record["source"] not in baseline else
                           "Paper added to archive" if record["source"] == "PubMed" else "Trial added to archive")
            elif record.get("status") != previous.get("status") or (record.get("has_results") and not previous.get("has_results")):
                details = []
                if record.get("status") != previous.get("status"):
                    details.append(f"Status changed from {previous.get('status')} to {record.get('status')}")
                if record.get("has_results") and not previous.get("has_results"):
                    details.append("Registry results first observed")
                summary = ". ".join(details)
            events.append({"summary": summary, "id": record["id"], "kind": "new" if previous is None else "updated",
                           "at": timestamp, "title": record["title"], "url": record["url"]})
    return sorted(merged.values(), key=lambda r: (r["changed_at"], r["id"]), reverse=True), events


def render(state, target):
    target.mkdir(parents=True, exist_ok=True)
    state = {**state, "drug_aliases": state.get("drug_aliases", {})}
    # JSON escaped for script element; record values are inserted only via textContent.
    payload = json.dumps(state, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    template = (ROOT / "templates/dashboard.html").read_text(encoding="utf-8")
    (target / "index.html").write_text(template.replace("__RADAR_DATA__", payload), encoding="utf-8")
    lines = ["# GLP-1 Evidence Radar", "", "Last collection (UTC): " + state["last_run"], ""]
    for name, health in state["sources"].items():
        lines.append(f"- {name}: {health['status']} ({health.get('returned', 0)} records returned)")
    lines += ["", "## Latest collection", ""]
    initial = sum(e.get("summary") == "Initial collection" for e in state["latest_changes"])
    if initial:
        lines.append(f"{initial} records in the initial collection.")
    for event in state["latest_changes"]:
        if event.get("summary") != "Initial collection":
            title = html.escape(event["title"]).replace("[", "&#91;").replace("]", "&#93;")
            lines.append(f"- {event.get('summary', event['kind'])}: [{title}]({event['url']})")
    if not state["latest_changes"]:
        lines.append("No changes observed in this run. See collection status above.")
    (target / "latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args):
    config = read_json(args.config)
    if not config or not config.get("drugs"):
        raise ValueError("Watchlist must contain drugs")
    if not 1 <= config["max_records_per_source"] <= 10000 or config["pubmed_days"] < 1:
        raise ValueError("Invalid source bounds")
    path = args.data / "catalog.json"
    state = read_json(path, {"records": [], "history": [], "sources": {}})
    timestamp, incoming, health = now(), [], {}
    if args.demo:
        # Demo mode has its own destination, cannot contaminate real data.
        demo = read_json(ROOT / "tests/fixtures/demo.json")
        incoming = demo["records"]
        health = {"DEMO": {"status": "synthetic", "returned": len(incoming), "truncated": False}}
    else:
        for name, collector in [("PubMed", pubmed), ("ClinicalTrials.gov", trials)]:
            try:
                rows, info = collector(config)
                incoming.extend(rows)
                health[name] = {"status": "ok", "checked_at": timestamp,
                                "last_success": timestamp, **info}
            except (HTTPError, URLError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                # A failed source preserves all of its prior records and last successful date.
                health[name] = {"status": "error", "checked_at": timestamp,
                                "last_success": state.get("sources", {}).get(name, {}).get("last_success"),
                                "error_type": type(exc).__name__}
                print(f"{name}: failed ({type(exc).__name__}); previous records retained")
    records, events = merge(state["records"], incoming, timestamp)
    state = {"schema_version": 1, "demo": args.demo, "last_run": timestamp,
             "drug_aliases": config["drugs"], "notice": NOTICE, "sources": health, "records": records,
             "latest_changes": events, "history": (events + state["history"])[:3000]}
    write_json(path, state)
    render(state, args.output)
    print(f"{len(records)} archived records; {len(events)} changes; report: {args.output / 'index.html'}")
    return 1 if any(s["status"] == "error" for s in health.values()) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/watchlist.json")
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--output", type=Path, default=ROOT / "reports")
    parser.add_argument("--demo", action="store_true", help="Synthetic offline preview (separate demo directories)")
    args = parser.parse_args()
    if args.demo:
        args.data, args.output = ROOT / "outputs/demo-data", ROOT / "outputs/demo"
    raise SystemExit(run(args))
