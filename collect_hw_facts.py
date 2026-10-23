#!/usr/bin/env python3
"""
collect_hw_facts.py

Usage:
    python3 collect_hw_facts.py
    sudo python3 collect_hw_facts.py     # needed for RAM (dmidecode)
    python3 collect_hw_facts.py --template my_template.json --output out.json
"""

import argparse
import copy
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone

TEMPLATE_FILE = "inventory_template.json"
OUTPUT_FILE = "inventory_template.json"


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def run(cmd, use_sudo=False):
    if use_sudo:
        cmd = ["sudo", "-n"] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return r.stdout
    except (FileNotFoundError, OSError):
        return ""


def which(name):
    return shutil.which(name) is not None


def is_root():
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


# ----------------------------------------------------------------------
# detectors
# ----------------------------------------------------------------------

def lscpu_field(pattern):
    for line in run(["lscpu"]).splitlines():
        if re.match(pattern, line):
            return line.partition(":")[2].strip()
    return ""


def detect_cpu():
    sockets = lscpu_field(r"Socket\(s\)")
    cores = lscpu_field(r"Core\(s\) per socket")
    logical = lscpu_field(r"CPU\(s\):")
    model = lscpu_field(r"Model name:")
    return (
        model,
        int(sockets) if sockets.isdigit() else 0,
        int(cores) if cores.isdigit() else 0,
        int(logical) if logical.isdigit() else (os.cpu_count() or 0),
    )


BMC_VGA = re.compile(r"aspeed|matrox|mgag200|cirrus|vmware|qxl|virtio", re.I)

def detect_gpu_count():
    if which("nvidia-smi"):
        out = run(["nvidia-smi", "-L"])
        n = sum(1 for l in out.splitlines() if l.startswith("GPU "))
        if n:
            return n
    if not which("lspci"):
        return 0
    return sum(
        1
        for l in run(["lspci"]).splitlines()
        if re.search(r"vga|3d controller|display controller", l, re.I)
        and not BMC_VGA.search(l)
    )


def find_dmidecode():
    return (shutil.which("dmidecode")
            or next((p for p in ("/usr/sbin/dmidecode", "/sbin/dmidecode")
                     if os.access(p, os.X_OK)), None))


def detect_ram():
    """Returns [(size_gb, module_count), ...] or [] if undetectable."""
    exe = find_dmidecode()
    if not exe:
        print("WARN: dmidecode not found (try: apt install dmidecode)", file=sys.stderr)
        return []

    # Use sudo -n if not root; works only if passwordless sudo is configured
    out = run([exe, "-t", "17"], use_sudo=not is_root())
    if not out.strip() or "Memory Device" not in out:
        print("WARN: dmidecode returned no memory data "
              "(run the script with sudo, or this is a VM/container)", file=sys.stderr)
        return []

    sizes, in_dev = [], False
    for line in out.splitlines():
        s = line.strip()
        if s == "Memory Device":
            in_dev = True
            continue
        if s == "":
            in_dev = False
            continue
        if in_dev and s.startswith("Size:"):
            m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(TB|GB|MB|TiB|GiB|MiB)\b",
                         s.split(":", 1)[1].strip(), re.IGNORECASE)
            if m:
                size = float(m.group(1))
                unit = m.group(2).upper()
                if unit in ("MB", "MIB"):
                    size /= 1024
                elif unit in ("TB", "TIB"):
                    size *= 1024
                sizes.append(size)
    if not sizes:
        print("WARN: dmidecode ran but no populated DIMMs were parsed", file=sys.stderr)
    return sorted(Counter(sizes).items())


def detect_storage():
    """Returns (hdd_count, [(size_tb, ssd_count), ...])."""
    if not which("lsblk"):
        return 0, []
    out = run(["lsblk", "-d", "-b", "-n", "-o", "NAME,ROTA,TYPE,SIZE"])
    hdd, ssd_sizes = 0, []
    for line in out.splitlines():
        p = line.split()
        if len(p) < 4 or p[2] != "disk":
            continue
        if p[1] == "1":
            hdd += 1
        else:
            try:
                ssd_sizes.append(round(int(p[3]) / 1000**4, 2))
            except ValueError:
                pass
    return hdd, sorted(Counter(ssd_sizes).items())


def detect_nic_ports():
    """Count physical network ports (excludes virtual interfaces and SR-IOV VFs)."""
    ports = 0
    for path in glob.glob("/sys/class/net/*"):
        dev = os.path.join(path, "device")
        if not os.path.exists(dev):
            continue                      # lo, docker0, veth*, bridges, bonds, VLANs
        if "/virtual/" in os.path.realpath(path):
            continue
        if os.path.exists(os.path.join(dev, "physfn")):
            continue                      # SR-IOV virtual function
        ports += 1
    return ports

def detect_fans():
    """Count fans from IPMI (servers) or hwmon (desktops/laptops). Returns 0 if unknown."""
    # 1) IPMI: lines like "Fan1 | 30h | ok | 29.1 | 8400 RPM"
    exe = shutil.which("ipmitool")
    if exe:
        out = run([exe, "sdr", "type", "Fan"], use_sudo=not is_root())
        names = set()
        for line in out.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 5:
                continue
            name, status, reading = parts[0], parts[2].lower(), parts[4]
            if status == "ns" or "no reading" in reading.lower():
                continue  # empty slot / not present
            # Dell dual-rotor fans appear as Fan1A/Fan1B -> count as one fan
            names.add(re.sub(r"(?<=\d)[A-Za-z]$", "", name))
        if names:
            return len(names)

    # 2) hwmon fallback
    inputs = glob.glob("/sys/class/hwmon/hwmon*/fan*_input")
    return len(inputs)

def detect_psus():
    exe = shutil.which("ipmitool")
    if not exe:
        return 0
    out = run([exe, "sdr", "type", "Power Supply"], use_sudo=not is_root())
    names = {
        line.split("|")[0].strip()
        for line in out.splitlines()
        if "presence detected" in line.lower()
    }
    return len(names)

def detect_install_date():
    """Filesystem birth time of / as YYYY-MM-DD, or '' if unavailable."""
    out = run(["stat", "-c", "%W", "/"]).strip()
    try:
        ts = int(out)
    except ValueError:
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

# ----------------------------------------------------------------------
# fill the template
# ----------------------------------------------------------------------

def set_if(d, key, value):
    """Overwrite only when the detected value is meaningful."""
    if value not in (None, "", 0, 0.0):
        d[key] = value


def fill(facts):
    model, sockets, cores, logical = detect_cpu()
    hdd, ssds = detect_storage()
    ram = detect_ram()

    if not facts.get("node_id"):
        facts["node_id"] = socket.gethostname()

    set_if(facts, "number_of_vcores", logical)

    set_if(facts["cpu"], "model", model)
    set_if(facts["cpu"], "number_of_cpus", sockets)
    set_if(facts["cpu"], "cores_per_cpu", cores)
    set_if(facts["cpu"], "vcores_per_machine", logical)
    set_if(facts, "installation_date", detect_install_date())

    # GPU / HDD / NIC / PSU: a detected 0 is a valid answer, so always write
    facts["gpu"]["number_of_gpus"] = detect_gpu_count()
    facts["hdd"]["number_of_hdds"] = hdd
    facts["network_cards"]["number_of_ports"] = detect_nic_ports()
    n_fans = detect_fans()
    if n_fans:
        facts["fans"]["number_of_fans"] = n_fans
    n_psu = detect_psus()
    if n_psu:
        facts["psu"]["number_per_device"] = n_psu

    # RAM: one entry per distinct module size, based on the template prototype
    if ram:
        proto = (facts.get("ramList") or [{}])[0]
        facts["ramList"] = []
        for size, n in ram:
            e = copy.deepcopy(proto)
            e["number_of_modules"] = n
            e["storage_per_module_gb"] = int(size) if float(size).is_integer() else round(size, 2)
            facts["ramList"].append(e)

    # SSDs: same approach
    if ssds:
        proto = (facts.get("ssdList") or [{}])[0]
        facts["ssdList"] = []
        for size, n in ssds:
            e = copy.deepcopy(proto)
            e["number_of_ssds"] = n
            e["storage_tb"] = size
            facts["ssdList"].append(e)
    elif not hdd and not ssds:
        pass  # keep template list untouched

    return facts


# ----------------------------------------------------------------------
# I/O
# ----------------------------------------------------------------------

def load_template(path):
    if not os.path.exists(path):
        sys.exit(f"ERROR: template not found: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        sys.exit(f"ERROR: template is not valid JSON ({path}): {exc}")

        
def write_json_atomic(data, path):
    path = os.path.abspath(path)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", default=TEMPLATE_FILE)
    ap.add_argument("--output", default=OUTPUT_FILE)
    args = ap.parse_args()

    facts = fill(load_template(args.template))

    try:
        write_json_atomic(facts, args.output)
    except Exception as exc:
        sys.exit(f"ERROR: could not write {args.output}: {exc}")

    print(f"Written to {args.output}")
    print(json.dumps(facts, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
