"""Turns hetzner-radar's published history into the seed of the record (README.md).

    mise exec -- python3 make_seed.py sb.duckdb > seed.jsonl.gz

`sb.duckdb` is https://static.radar.iodev.org/sb.duckdb. One line an offer:
its id, kind, whether it is ECC, when it was first and last seen, and its
first, lowest and last price. Needs the `duckdb` CLI that mise.toml pins.
"""

import gzip
import json
import subprocess
import sys

from watch import kind_of

QUERY = """
SELECT id,
       arg_max(cpu, seen) AS cpu,
       arg_max(ram_size, seen) AS ram_gb,
       arg_max(is_ecc, seen) AS ecc,
       arg_max(nvme_drives, seen) AS nvme,
       arg_max(sata_drives, seen) AS sata,
       arg_max(hdd_drives, seen) AS hdd,
       arg_max(with_gpu, seen) AS gpu,
       strftime(min(seen), '%Y-%m-%dT%H:%M:%SZ') AS first,
       strftime(max(seen), '%Y-%m-%dT%H:%M:%SZ') AS last,
       arg_min(price, seen) AS first_price,
       min(price) AS low,
       arg_max(price, seen) AS price
FROM server
GROUP BY id
ORDER BY id
"""


def main(path):
    out = subprocess.run(["duckdb", "-readonly", "-jsonlines", path, "-c", QUERY],
                         check=True, capture_output=True, text=True).stdout
    lines = []
    for row in map(json.loads, out.splitlines()):
        if int(row["id"]) >= 2**63:
            continue    # 19 ids of the file are not Hetzner's: past SQLite's integer
        offer = {"cpu": row["cpu"], "ram_gb": row["ram_gb"], "gpu": row["gpu"],
                 "nvme": row["nvme"] or [], "sata": row["sata"] or [], "hdd": row["hdd"] or []}
        lines.append(json.dumps({
            "id": int(row["id"]), "kind": kind_of(offer), "ecc": bool(row["ecc"]),
            "first": row["first"], "last": row["last"],
            "first_price": row["first_price"], "low": row["low"], "price": row["price"],
        }, separators=(",", ":")))
    # mtime=0: the same input gives the same bytes.
    sys.stdout.buffer.write(gzip.compress(("\n".join(lines) + "\n").encode(), 9, mtime=0))
    print(f"{len(lines)} offers", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
