"""Build src/jobhunt/data/cities_us_ca.tsv from GeoNames cities1000 (CC BY 4.0).

Usage: python scripts/build_cities.py
Columns: normalized ascii name, country (US|CA), admin1 (state postal code / province
abbreviation). Normalization must match classify._norm_city.
"""

from __future__ import annotations

import io
import re
import urllib.request
import zipfile
from pathlib import Path

URL = "https://download.geonames.org/export/dump/cities1000.zip"
OUT = Path(__file__).resolve().parent.parent / "src" / "jobhunt" / "data" / "cities_us_ca.tsv"
CA_ADMIN1 = {
    "01": "AB", "02": "BC", "03": "MB", "04": "NB", "05": "NL", "07": "NS", "08": "ON",
    "09": "PE", "10": "QC", "11": "SK", "12": "YT", "13": "NT", "14": "NU",
}


def norm_city(name: str) -> str:
    s = re.sub(r"[.'’`]", "", name.lower())
    s = re.sub(r"[\s\-]+", " ", s).strip()
    return re.sub(r"^st ", "saint ", s)


def main() -> None:
    with urllib.request.urlopen(URL, timeout=120) as r:  # noqa: S310 - fixed https URL
        data = r.read()
    rows: set[tuple[str, str, str]] = set()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        with z.open("cities1000.txt") as f:
            for line in io.TextIOWrapper(f, encoding="utf-8"):
                p = line.rstrip("\n").split("\t")
                cc, admin1 = p[8], p[10]
                if cc == "US":
                    pass
                elif cc == "CA":
                    admin1 = CA_ADMIN1.get(admin1, "")
                else:
                    continue
                if not admin1:
                    continue
                for name in {p[2], p[1]}:
                    n = norm_city(name)
                    if n and n.isascii():
                        rows.add((n, cc, admin1))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(f"{n}\t{c}\t{a}\n" for n, c, a in sorted(rows)), encoding="utf-8")
    print(f"wrote {len(rows)} rows to {OUT}")


if __name__ == "__main__":
    main()
