# Readme file for collecting the inventory data from nodes


## File overview and structure

### collect_hw_facts.py 

`collect_hw_facts.py` scrapes the node information and populates the `inventory_template.json` file

Usage:
    `python3 collect_hw_facts.py`
    `sudo python3 collect_hw_facts.py`     # needed for RAM (dmidecode)
    `python3 collect_hw_facts.py --template my_template.json --output out.json`



### send_hw_facts.py

`send_hw_facts.py` Sends filled inventory json to AUDITOR

1. Reads the inventory JSON from collect_hw_facts.py
2. POSTs it to the Digital Carbon Footprint API if you enable it in the config to compute the embedded carbon emission
3. Sends the inventory json to AUDITOR

Modify the `config.yaml` to set the AUDITOR endpoint and `node_ids` lists the nodes that `send_hw_facts.py` sends the inventory for.

For each expanded node name, the script sends a copy of the inventory with `node_id` set to that name. If `node_ids` is empty, a single item is sent with the `node_id` from the inventory file.

```yaml
auditor:
  node_ids:
    - n[2119-2130].node.uni.de     # expression -> 12 nodes
    - n01.node.uni.de          # plain name -> 1 node
```

Usage:
    `python3 send_hw_facts.py`
    `python3 send_hw_facts.py --dry-run`     # show payload only
    `python3 send_hw_facts.py --inventory-file inventory_template.json`



### Detected Fields

| JSON field                      | Source                                                        | Root needed | Notes                                                                            |
|---------------------------------|---------------------------------------------------------------|-------------|----------------------------------------------------------------------------------|
| `node_id`                       | `socket.gethostname()`                                        | No          | Only set if empty in the template                                                |
| `number_of_vcores`              | `lscpu` (`CPU(s)`), falls back to `os.cpu_count()`            | No          | Keeps template value if 0                                                        |
| `cpu.model`                     | `lscpu` (`Model name`)                                        | No          | Keeps template value if empty                                                    |
| `cpu.number_of_cpus`            | `lscpu` (`Socket(s)`)                                         | No          | Keeps template value if 0                                                        |
| `cpu.cores_per_cpu`             | `lscpu` (`Core(s) per socket`)                                | No          | Keeps template value if 0                                                        |
| `cpu.vcores_per_machine`        | `lscpu` (`CPU(s)`)                                            | No          | Keeps template value if 0                                                        |
| `gpu.number_of_gpus`            | `nvidia-smi -L`, else `lspci` (BMC chips filtered)            | No          | Always written; 0 is valid. Integrated GPUs counted                              |
| `hdd.number_of_hdds`            | `lsblk -d` (`ROTA=1`, type `disk`)                            | No          | Always written; 0 is valid                                                       |
| `ssdList[]` count and size      | `lsblk -d -b` (`ROTA=0`), one entry per size                  | No          | Size in TB (1000^4 bytes). Template list kept if none found                      |
| `ramList[]` count and size      | `dmidecode -t 17` (`Size:` lines), one entry per size         | Yes         | Template list kept if undetectable (VM, container)                               |
| `network_cards.number_of_ports` | `/sys/class/net/*` (physical only, no virtual or SR-IOV VFs)  | No          | Includes Wi-Fi and USB adapters unless filtered                                  |
| `fans.number_of_fans`           | `ipmitool sdr type Fan`, else `/sys/class/hwmon/*/fan*_input` | Yes (IPMI)  | Counts sensors, not physical fans. Keeps template value if 0                     |
| `psu.number_per_device`         | `ipmitool sdr type "Power Supply"` (presence detected)        | Yes         | Keeps template value if 0                                                        |
| `installation_date`             | `stat -c %W /` (filesystem birth time, UTC)                   | No          | OS install date, not hardware purchase date. Keeps template value if unsupported |

### Not collected (fill in manually)

| JSON field                                              | Where to get it                                                              |
|---------------------------------------------------------|------------------------------------------------------------------------------|
| `site_id`, `group`, `queue_id`                          | Site / scheduler configuration                                               |
| `pue`, `wue`, `availability`                            | Site operations                                                              |
| `emission_co2_per_kwh`, `planned_usage_years`           | Grid data / site policy                                                      |
| `power_consumption.*`                                   | Measure with `ipmitool dcmi power reading` (idle / `stress-ng`) and HEPScore |
| `cpu.tdp_watt`, `cpu.thermal_design_power_per_cpu_watt` | Vendor spec sheet by model                                                   |
| `gpu.tdp_watt`                                          | Vendor spec sheet by model                                                   |
| `*.die_size_cm2`                                        | Vendor / teardown data by model                                              |
| `*.with_gold_pins`, `gpu.with_gold_connectors`          | Vendor / teardown data by model                                              |
| `cpu.heat_sink_kg`                                      | Vendor / teardown data by model                                              |
| `fans.weight_per_fan_kg`, `psu.weight_per_psu_kg`       | Spec sheet or physical measurement                                           |
| `cables.total_weight_kg`, `housing.*`                   | Spec sheet or physical measurement                                           |
| `circuit_boards.area_cm2`                               | Spec sheet or physical measurement                                           |
| `embedded_co2_eq_kg`, `embedded_co2_eq_kg_updated_at`   | Computed downstream from the filled-in inventory                             |
| `embedded_co2_per_runtime`                              | Computed downstream from the filled-in inventory                             |

