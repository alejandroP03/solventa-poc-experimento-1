#!/usr/bin/env python3
"""Genera datos sinteticos deterministas y precarga Redis para SP-3."""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "mock-openfinance"))

from app.profiles import build_profile  # noqa: E402

PRODUCTS = ("VIAJE", "DISPOSITIVO", "VIDA_MICRO")
CITIES = ("Bogota", "Medellin", "Cali", "Barranquilla", "Bucaramanga")
PARTNERS = ("partner-a", "partner-b", "partner-c")
SEED = 20260903
FIELD_CACHED_AT = "_cached_at"


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.split("#", 1)[0].strip().strip('"')
    return values


def build_dataset(size: int) -> list[dict]:
    rng = random.Random(SEED)
    rows = []
    for idx in range(size):
        product = PRODUCTS[idx % len(PRODUCTS)]
        rows.append(
            {
                "client_id": f"CLI-{idx + 1:04d}",
                "product_code": product,
                "insured_amount": rng.choice((500_000, 1_000_000, 2_000_000, 5_000_000)),
                "age": rng.randint(18, 72),
                "city": CITIES[idx % len(CITIES)],
                "partner_id": PARTNERS[idx % len(PARTNERS)],
            }
        )
    return rows


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=check)


def redis_exec(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return run(["docker", "compose", "exec", "-T", "redis", "redis-cli", *args], check=check)


def purge_cache() -> int:
    scan = redis_exec(["--scan", "--pattern", "solventa:profile:*"], check=False)
    keys = [line.strip() for line in scan.stdout.splitlines() if line.strip()]
    if not keys:
        return 0
    deleted = 0
    for key in keys:
        redis_exec(["DEL", key])
        deleted += 1
    return deleted


def preload(rows: list[dict], ratio: float, ttl_s: int, grace_s: int) -> int:
    if ratio < 0 or ratio > 1:
        raise ValueError("CACHE_PRELOAD_RATIO debe estar entre 0.0 y 1.0")

    purge_cache()

    rng = random.Random(SEED)
    selected = list(rows)
    rng.shuffle(selected)
    selected = selected[: int(len(rows) * ratio)]

    for row in selected:
        profile = build_profile(row["client_id"])
        profile[FIELD_CACHED_AT] = time.time()
        payload = json.dumps(profile, separators=(",", ":"))
        redis_exec(["SET", f"solventa:profile:{row['client_id']}", payload, "EX", str(ttl_s + grace_s)])

    return len(selected)


def main() -> int:
    env_file = load_env_file(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--size",
        type=int,
        default=int(env_file.get("SEED_SIZE", os.getenv("SEED_SIZE", "1000"))),
    )
    parser.add_argument(
        "--ratio",
        type=float,
        default=float(env_file.get("CACHE_PRELOAD_RATIO", os.getenv("CACHE_PRELOAD_RATIO", "0.5"))),
    )
    parser.add_argument("--ttl-s", type=int, default=int(env_file.get("PROFILE_CACHE_TTL_S", "300")))
    parser.add_argument(
        "--stale-grace-s",
        type=int,
        default=int(env_file.get("PROFILE_CACHE_STALE_GRACE_S", "1800")),
    )
    parser.add_argument("--output", default=str(ROOT / "load" / "k6" / "data" / "quotes.json"))
    parser.add_argument("--preload", action="store_true")
    parser.add_argument("--purge-only", action="store_true")
    args = parser.parse_args()

    if args.purge_only:
        deleted = purge_cache()
        print(json.dumps({"purged": deleted}, indent=2))
        return 0

    rows = build_dataset(args.size)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")

    result = {"rows": len(rows), "output": str(output), "preloaded": None}
    if args.preload:
        result["preloaded"] = preload(rows, args.ratio, args.ttl_s, args.stale_grace_s)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
