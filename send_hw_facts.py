#!/usr/bin/env python3
"""
send_hw_facts.py

1. Reads the inventory JSON from collect_hw_facts.py
2. POSTs it to the Digital Carbon Footprint API if it is enabled in the config.yaml
3. Writes the result back into the inventory JSON:
     embedded_co2_eq_kg, embedded_co2_eq_kg_updated_at,
     embedded_co2_per_runtime (g CO2eq per vcore-hour)
4. Sends the inventory JSON to AUDITOR endpoint /inventory

Usage:
    python3 send_hw_facts.py
    python3 send_hw_facts.py --dry-run     # show payload only
    python3 send_hw_facts.py --inventory-file inventory_template.json
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
import copy

try:
    import yaml
except ImportError:
    print("ERROR: pip install pyyaml --break-system-packages", file=sys.stderr)
    sys.exit(1)

HOURS_PER_YEAR = 8760
RETRY_BACKOFF_SECONDS = 2


# ----------------------------------------------------------------------
# file helpers
# ----------------------------------------------------------------------

def load_json_file(path):
    p = Path(path)
    if not p.exists():
        sys.exit(f"ERROR: facts file not found: {path}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"ERROR: {path} is not valid JSON: {exc}")


def load_yaml_file(path):
    p = Path(path)
    if not p.exists():
        sys.exit(f"ERROR: config file not found: {path}")
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        sys.exit(f"ERROR: {path} is not valid YAML: {exc}")


def write_json_atomic(data, path):
    path = os.path.abspath(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ----------------------------------------------------------------------
# inventory JSON -> API request
# ----------------------------------------------------------------------

def build_payload(facts):
    cpu = facts.get("cpu", {})
    gpu = facts.get("gpu", {})

    release_year = facts.get("release_year")
    if not release_year:
        # fall back to installation year
        try:
            release_year = int(str(facts.get("installation_date", ""))[:4])
        except ValueError:
            release_year = 0

    node_id = facts.get("node_id", "")

    ssd = facts.get("ssdList", [])
    if isinstance(ssd, dict):          
        ssd = [ssd]

    return {
        "site_id": facts.get("node_id", ""),
        "overall_device": {
            "device_name": node_id,
            "release_year": release_year,
        },
        "device_name": node_id,
        "cpu": {
            "number_of_cpus": cpu.get("number_of_cpus", 0),
            "cores_per_cpu": cpu.get("cores_per_cpu", 0),
            "tdp_watt": cpu.get("tdp_watt", 0),
            "die_size_cm2": cpu.get("die_size_cm2", 0),
            "with_gold_pins": cpu.get("with_gold_pins", True),
            "heatsink_weight_kg": cpu.get("heat_sink_kg", 0),   # renamed
        },
        "gpu": {
            "number_of_gpus": gpu.get("number_of_gpus", 0),
            "tdp_watt": gpu.get("tdp_watt", 0),
            "die_size_cm2": gpu.get("die_size_cm2", 0),
            "with_gold_pins": gpu.get("with_gold_connectors", True),  # renamed
        },
        "ramList": [
            {
                "number_of_modules": r.get("number_of_modules", 0),
                "storage_per_module_gb": r.get("storage_per_module_gb", 0),
                "die_size_cm2": r.get("die_size_cm2", 0),
                "with_gold_pins": r.get("with_gold_pins", True),
            }
            for r in facts.get("ramList", [])
        ],
        "hdd": {"number_of_hdds": facts.get("hdd", {}).get("number_of_hdds", 0)},
        "ssdList": [
            {
                "number_of_ssds": s.get("number_of_ssds", 0),
                "storage_tb": s.get("storage_tb", 0),
                "die_size_cm2": s.get("die_size_cm2", 0),
            }
            for s in ssd
        ],
        "network_cards": {
            "number_of_ports": facts.get("network_cards", {}).get("number_of_ports", 0)
        },
        "circuit_boards": {
            "area_cm2": facts.get("circuit_boards", {}).get("area_cm2", 0)
        },
        "psu": {
            "number_per_device": facts.get("psu", {}).get("number_per_device", 0),
            "weight_per_psu_kg": facts.get("psu", {}).get("weight_per_psu_kg", 0),
        },
        "cables": {
            "total_weight_kg": facts.get("cables", {}).get("total_weight_kg", 0)
        },
        "fans": {
            "number_of_fans": facts.get("fans", {}).get("number_of_fans", 0),
            "weight_per_fan_kg": facts.get("fans", {}).get("weight_per_fan_kg", 0),
        },
        "housing": {
            k: facts.get("housing", {}).get(k, 0)
            for k in (
                "total_device_weight_without_psus_kg",
                "steel_kg",
                "copper_kg",
                "plastics_kg",
                "aluminium_kg",
            )
        },
    }


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------

def build_headers(api):
    headers = dict(api.get("headers", {}))
    headers.setdefault("accept", "application/json")
    headers.setdefault("Content-Type", "application/json")
    if api.get("api_key"):
        name = api.get("api_key_header", "Authorization")
        headers[name] = f"{api.get('api_key_prefix', '')}{api['api_key']}"
    return headers


def post_facts(payload, api):
    url = api["url"]
    timeout = api.get("timeout_seconds", 30)
    retries = max(1, api.get("retries", 1))

    data = json.dumps(payload).encode("utf-8")
    headers = build_headers(api)
    last_error = None

    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if 400 <= exc.code < 500:
                return exc.code, body
            last_error = f"HTTP {exc.code}: {body}"
        except urllib.error.URLError as exc:
            last_error = str(exc.reason)

        if attempt < retries:
            print(f"Attempt {attempt}/{retries} failed ({last_error}); "
                  f"retrying in {RETRY_BACKOFF_SECONDS}s...", file=sys.stderr)
            time.sleep(RETRY_BACKOFF_SECONDS)

    sys.exit(f"ERROR: all {retries} attempt(s) failed: {last_error}")

# ----------------------------------------------------------------------
# API response -> inventory JSON
# ----------------------------------------------------------------------

def dig(obj, dotted):
    for part in dotted.split("."):
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def extract_co2_kg(response, api):
    """GWP total (kg CO2 eq) from the API response."""
    path = api.get("response_co2_path", "GWP.total")
    v = dig(response, path)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    print(f"WARNING: '{path}' not found/numeric in response", file=sys.stderr)
    return None


def embedded_per_runtime(co2_kg, facts):
    """g CO2eq per vcore-hour, over planned lifetime, scaled by availability."""
    vcores = facts.get("number_of_vcores") or 0
    years = facts.get("planned_usage_years") or 0
    avail = facts.get("availability") or 0
    denom = years * HOURS_PER_YEAR * avail * vcores
    if denom <= 0:
        return None
    return round(co2_kg * 1000 / denom, 6)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def expand_hostlist(expr):
    """Expand Slurm-style host list expressions, e.g.
         "hk2n[1005-1006,1026-1036].localdomain"
       Zero padding is kept ("hkn[0013,0017-0020]"). Several bracket groups
       are expanded as a cross product. Plain names pass through unchanged."""
    m = re.search(r"\[([^\]]+)\]", expr)
    if not m:
        return [expr]
    head, tail = expr[:m.start()], expr[m.end():]
    out = []
    for part in m.group(1).split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            width = len(lo) if lo.startswith("0") else 0
            nums = [str(n).zfill(width) for n in range(int(lo), int(hi) + 1)]
        else:
            nums = [part]
        for n in nums:
            out.extend(expand_hostlist(f"{head}{n}{tail}"))
    return out


def build_auditor_payload(facts, auditor_cfg):
    """Body for the auditor POST.

    node_ids from config.yaml are expanded, and one item is built per node:
    a copy of the inventory facts with node_id set to that node.
    If no node_ids are configured, a single item is sent with the node_id
    already stored in the inventory file.
    """
    raw = auditor_cfg.get("node_ids") or []
    if isinstance(raw, str):
        raw = [raw]

    node_ids = []
    for entry in raw:
        for name in expand_hostlist(str(entry).strip()):
            if name not in node_ids:          # de-duplicate, keep order
                node_ids.append(name)

    if not node_ids:
        own = facts.get("node_id")
        if not own:
            sys.exit("ERROR: config.auditor.node_ids is empty and the "
                     "inventory file has no node_id")
        node_ids = [own]

    items = []
    for name in node_ids:
        item = copy.deepcopy(facts)
        item["node_id"] = name
        items.append(item)

    return {"node_ids": node_ids, "items": items}


def send_auditor(facts, config):
    auditor = config.get("auditor") or {}
    if not auditor.get("url"):
        print("No auditor.url in config.yaml, skipping auditor request.")
        return
    payload = build_auditor_payload(facts, auditor)
    status, body = post_facts(payload, auditor)
    print(f"\nAuditor request: HTTP {status}")
    try:
        print(json.dumps(json.loads(body), indent=2))
    except json.JSONDecodeError:
        print(body)
    if status >= 400:
        sys.exit(3)


def run_carbonfootprint(facts, cf, facts_file):
    """Send the carbon footprint request and store the result in `facts`."""
    if not cf.get("url"):
        sys.exit("ERROR: config.carbonfootprint.url is not set")

    payload = build_payload(facts)
    status, body = post_facts(payload, cf)
    print(f"HTTP {status}")
    try:
        response = json.loads(body)
        print(json.dumps(response, indent=2))
    except json.JSONDecodeError:
        print(body)
        response = None

    if status >= 400 or response is None:
        sys.exit(2)

    co2 = extract_co2_kg(response, cf)
    if co2 is None:
        facts["embedded_co2_api_response"] = response 
        write_json_atomic(facts, facts_file)
        sys.exit("ERROR: GWP.total not found in response (raw response saved in the facts file).")

    facts["embedded_co2_eq_kg"] = co2
    facts["embedded_co2_eq_kg_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    facts["embedded_co2_per_runtime"] = embedded_per_runtime(co2, facts)
    facts.pop("embedded_co2_api_response", None)

    write_json_atomic(facts, facts_file)
    print(f"\nUpdated {facts_file}: embedded_co2_eq_kg={co2}, "
          f"embedded_co2_per_runtime={facts['embedded_co2_per_runtime']} g/vcore-h")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory-file", default="inventory_template.json")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    facts = load_json_file(args.inventory_file)
    config = load_yaml_file(args.config)

    cf = config.get("carbonfootprint") or {}
    cf_enabled = cf.get("enabled", False)
    auditor = config.get("auditor") or {}

    if args.dry_run:
        if cf_enabled:
            print(json.dumps(build_payload(facts), indent=2))
        else:
            print("carbonfootprint request disabled in config (enabled: false).")
        if auditor.get("url"):
            print("\n--- auditor request ---")
            print(json.dumps(build_auditor_payload(facts, auditor), indent=2))
        return

    if cf_enabled:
        run_carbonfootprint(facts, cf, args.inventory_file)
    else:
        print("Carbon footprint request disabled "
              "(carbonfootprint.enabled: false); skipping.")

    # Second request: reached if the first succeeded or was disabled
    send_auditor(facts, config)


if __name__ == "__main__":
    main()
