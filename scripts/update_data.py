#!/usr/bin/env python3
"""Rebuild data.json for the NYC Smooth Streets Map from NYC Open Data.

Steps (the same ones done by hand before):
  1. Download every row of the Street Pavement Ratings dataset (6yyb-pb25).
  2. Keep only the newest inspection for each street segment.
  3. Pack ratings, names and simplified street lines into one compact JSON file.

Usage:
  python scripts/update_data.py data.json                 # download from NYC Open Data
  python scripts/update_data.py data.json --from-csv x.csv # use a CSV you already have

Standard library only. Set NYC_APP_TOKEN (optional) to raise the API's rate limit.
"""
import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://data.cityofnewyork.us/resource/6yyb-pb25.csv"
PAGE = 50000
BOROS = ["MANHATTAN", "BRONX", "BROOKLYN", "QUEENS", "STATEN ISLAND"]
LON0, LAT0 = -74.26, 40.49   # south-west corner of NYC; keeps packed integers positive
TOL = 0.0000135               # line simplification tolerance in degrees (about 5 feet)

# Column names differ between the website's CSV export ("OnStreetName") and the
# API ("onstreetname"). Headers are compared lower-cased with punctuation removed.
COLUMNS = {
    "geom":   ["thegeom", "geom", "geometry", "locationgeometry"],
    "oft":    ["oftcode"],
    "boro":   ["boroughname", "borough", "boro"],
    "on":     ["onstreetname", "onstreet"],
    "from":   ["fromstreetname", "fromstreet"],
    "to":     ["tostreetname", "tostreet"],
    "rating": ["systemrating", "rating"],
    "date":   ["inspectiontime", "inspectiondate", "ratingdate", "inspectiondatetime"],
    "length": ["locationgeometrystlength", "stlength", "shapeleng", "shapelength", "length"],
}
REQUIRED = ["geom", "boro", "on", "from", "to", "rating", "date"]


def squash(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_columns(header):
    by_squashed = {squash(h): h for h in header}
    found = {}
    for key, options in COLUMNS.items():
        for opt in options:
            if opt in by_squashed:
                found[key] = by_squashed[opt]
                break
    missing = [k for k in REQUIRED if k not in found]
    if missing:
        sys.exit(f"ERROR: could not find columns for {missing}.\nColumns in the data: {header}\n"
                 "Add the right names to COLUMNS in scripts/update_data.py.")
    return found


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().upper())


def parse_date(s):
    """Accepts 2025-12-31T00:00:00.000, 12/31/2025 12:00:00 AM, 2026/10/31, 2026-10-31."""
    s = (s or "").strip()
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    return None


def parse_geom(text):
    """WKT (MULTI)LINESTRING, or a GeoJSON geometry string, to [[lon, lat], ...] lines."""
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("{"):
        g = json.loads(text)
        coords = g.get("coordinates") or []
        lines = coords if g.get("type") == "MultiLineString" else [coords]
        return [[[float(x), float(y)] for x, y, *_ in line] for line in lines if len(line) >= 2]
    lines = []
    for part in re.findall(r"\(([^()]+)\)", text):
        pts = []
        for pair in part.split(","):
            xy = pair.split()
            if len(xy) >= 2:
                pts.append([float(xy[0]), float(xy[1])])
        if len(pts) >= 2:
            lines.append(pts)
    return lines


def line_feet(lines):
    total = 0.0
    for line in lines:
        for (x1, y1), (x2, y2) in zip(line, line[1:]):
            a = math.radians(y1)
            dx = math.radians(x2 - x1) * math.cos(a) * 20902231
            dy = math.radians(y2 - y1) * 20902231
            total += math.hypot(dx, dy)
    return total


def simplify(pts):
    """Douglas-Peucker on [lon, lat] points."""
    if len(pts) < 3:
        return pts
    (x1, y1), (x2, y2) = pts[0], pts[-1]
    dx, dy = x2 - x1, y2 - y1
    den = math.hypot(dx, dy)
    best, idx = -1.0, 0
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        d = abs(dy * px - dx * py + x2 * y1 - y2 * x1) / den if den else math.hypot(px - x1, py - y1)
        if d > best:
            best, idx = d, i
    if best <= TOL:
        return [pts[0], pts[-1]]
    return simplify(pts[: idx + 1])[:-1] + simplify(pts[idx:])


def api_pages():
    """Yield the dataset's CSV text one page at a time."""
    headers = {"User-Agent": "nyc-smooth-streets-updater", "Accept": "text/csv"}
    token = os.environ.get("NYC_APP_TOKEN")
    if token:
        headers["X-App-Token"] = token
    offset = 0
    while True:
        query = urllib.parse.urlencode({"$limit": PAGE, "$offset": offset, "$order": ":id"})
        for attempt in range(4):
            try:
                req = urllib.request.Request(f"{API}?{query}", headers=headers)
                with urllib.request.urlopen(req, timeout=180) as resp:
                    text = resp.read().decode("utf-8")
                break
            except Exception as e:  # network hiccup: wait and retry
                if attempt == 3:
                    sys.exit(f"ERROR: download failed at row {offset}: {e}")
                time.sleep(10 * (attempt + 1))
        rows = text.count("\n")
        print(f"  downloaded rows {offset:,} to {offset + max(rows - 1, 0):,}", flush=True)
        yield text
        if rows <= 1:  # header only: no more data
            return
        offset += PAGE


def read_rows(from_csv):
    """Yield (header, row dict) pairs from a local CSV or from the API."""
    csv.field_size_limit(10**9)
    if from_csv:
        with open(from_csv, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                yield reader.fieldnames, row
        return
    for text in api_pages():
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            yield reader.fieldnames, row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--from-csv")
    ap.add_argument("--min-segments", type=int, default=50000,
                    help="refuse to write a file with fewer segments than this (protects the live map)")
    args = ap.parse_args()

    cols = None
    latest = {}
    source_rows = 0
    skipped = {"no rating": 0, "no date": 0}
    for header, row in read_rows(args.from_csv):
        if cols is None:
            cols = find_columns(header)
            print("columns used:", cols)
        source_rows += 1
        try:
            rating = float(row[cols["rating"]])
        except (TypeError, ValueError):
            skipped["no rating"] += 1
            continue
        when = parse_date(row[cols["date"]])
        if when is None:
            skipped["no date"] += 1
            continue
        oft = (row.get(cols["oft"], "") if "oft" in cols else "").strip()
        names = (norm(row[cols["boro"]]), norm(row[cols["on"]]), norm(row[cols["from"]]), norm(row[cols["to"]]))
        key = oft if oft and "E+" not in oft.upper() else "|".join(names[:2] + tuple(sorted(names[2:])))
        old = latest.get(key)
        if old is None or when >= old["when"]:  # newest inspection wins; ties keep the later row
            latest[key] = {"when": when, "rating": rating, "names": names,
                           "geom": row[cols["geom"]], "length": row.get(cols["length"], "") if "length" in cols else ""}

    if cols is None:
        sys.exit("ERROR: the data had no rows.")
    print(f"source rows: {source_rows:,}; segments after keeping the newest inspection: {len(latest):,}")

    name_id, names_out = {}, []

    def nid(s):
        if s not in name_id:
            name_id[s] = len(names_out)
            names_out.append(s)
        return name_id[s]

    out = {k: [] for k in ("rating", "len", "boro", "on", "from", "to", "g")}
    skipped.update({"no geometry": 0, "no borough": 0})
    dates = []
    for seg in latest.values():
        boro, on, frm, to = seg["names"]
        if boro not in BOROS:
            skipped["no borough"] += 1
            continue
        try:
            lines = parse_geom(seg["geom"])
        except (ValueError, json.JSONDecodeError):
            lines = []
        if not lines:
            skipped["no geometry"] += 1
            continue
        try:
            length = float(str(seg["length"]).replace(",", ""))
        except ValueError:
            length = line_feet(lines)
        out["rating"].append(round(seg["rating"] * 100))
        out["len"].append(round(length))
        out["boro"].append(BOROS.index(boro))
        out["on"].append(nid(on))
        out["from"].append(nid(frm))
        out["to"].append(nid(to))
        parts = [simplify(line) for line in lines]
        out["g"].append(len(parts))
        for part in parts:
            out["g"].append(len(part))
            px = py = None
            for x, y in part:
                qx, qy = round((x - LON0) * 1e5), round((y - LAT0) * 1e5)
                out["g"].extend((qx, qy) if px is None else (qx - px, qy - py))
                px, py = qx, qy
        dates.append(seg["when"])

    count = len(out["rating"])
    print(f"segments packed: {count:,}; skipped: {skipped}")
    if count < args.min_segments:
        sys.exit(f"ERROR: only {count:,} segments (minimum {args.min_segments:,}). Not writing {args.out}.")
    if os.path.exists(args.out):
        try:
            previous = json.load(open(args.out)).get("meta", {}).get("segments", 0)
        except (ValueError, OSError):
            previous = 0
        if previous and count < 0.9 * previous:
            sys.exit(f"ERROR: {count:,} segments is more than 10% below the previous {previous:,}. Not writing.")

    out.update({
        "lon0": LON0, "lat0": LAT0, "boros": BOROS, "names": names_out,
        "meta": {
            "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
            "source": "NYC Open Data, Street Pavement Ratings (6yyb-pb25)" + (" (local CSV)" if args.from_csv else ""),
            "sourceRows": source_rows,
            "segments": count,
            "notDrawn": skipped["no geometry"] + skipped["no borough"],
            "inspectedFrom": min(dates).isoformat(),
            "inspectedTo": max(dates).isoformat(),
        },
    })
    with open(args.out, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
