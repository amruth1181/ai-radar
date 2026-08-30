"""The daily run. Six steps, in order.

    1. ingest              -> raw.items          fail soft per source
    2. dbt run staging+int -> int_items_dedup
    3. dbt test int        -> HARD FAIL on duplicates
    4. enrich              -> raw.enrichments    partial failure tolerated
    5. dbt run marts       -> fct_daily_digest
    6. deliver + ledger    -> Discord

Step 3 failing hard is deliberate. A duplicate bug should stop the pipeline, not ship
a broken digest -- once you see the same item twice you stop trusting the whole thing.

Everything else fails soft. A dead feed, a partly failed enrichment run, or one unscored
item is not a reason to send nothing: silence is indistinguishable from a crashed job,
so the digest ships with the problem named in its footer.

    uv run python scripts/run_daily.py
    uv run python scripts/run_daily.py --skip-ingest --dry-run
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings  # noqa: E402
from scripts.init_warehouse import init  # noqa: E402

log = logging.getLogger("run_daily")

TRANSFORM_DIR = settings.REPO_ROOT / "transform"


class StepFailed(RuntimeError):
    """A step that must stop the pipeline."""


def run_dbt(*args: str) -> None:
    """Invoke the project's dbt, not whatever is on PATH.

    The global binary here is dbt-fusion, a different engine with its own incremental
    behaviour. `uv run` guarantees the version the models were written against.
    """
    target = settings.get("DBT_TARGET", "dev")
    command = ["uv", "run", "dbt", *args, "--profiles-dir", ".", "--target", target]
    log.info("$ %s", " ".join(command))
    result = subprocess.run(command, cwd=TRANSFORM_DIR)
    if result.returncode != 0:
        raise StepFailed(f"dbt {' '.join(args)} failed (exit {result.returncode})")


def step_ingest() -> list[tuple[str, str]]:
    from ingest.pipeline import run as ingest_run

    report = ingest_run(destination="duckdb" if settings.get("DBT_TARGET", "dev") == "dev" else "bigquery")
    log.info("ingest: %s", report.summary())
    for name, reason in report.failures:
        log.warning("source %s failed: %s", name, reason)
    return report.failures


def step_enrich() -> int:
    """Score what is unscored. Returns how many items could not be scored."""
    from enrich.run import enrich_pending
    from warehouse import get_warehouse

    with get_warehouse() as wh:
        result = enrich_pending(wh)
        log.info("enrich: %s", result.summary())
        return result.failed


def step_deliver(failed_sources, unscored, dry_run: bool) -> bool:
    from deliver import discord
    from deliver.digest import build_digest, record_sent
    from warehouse import get_warehouse

    with get_warehouse() as wh:
        digest = build_digest(wh, failed_sources=failed_sources)
        digest.unscored = unscored

        if dry_run:
            content, embeds = discord.render(digest)
            print(content)
            for embed in embeds:
                print(f"  · {embed['title'][:70]}")
            return False

        sent = discord.send_digest(digest)
        # Ledger only after delivery is confirmed. Recording first would silently
        # drop a digest whose send failed -- those items would never be eligible again.
        if sent:
            record_sent(digest, channel="discord", wh=wh)
        return sent


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full daily pipeline.")
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--skip-enrich", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="render but do not send")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    started = time.monotonic()
    target = settings.get("DBT_TARGET", "dev")
    log.info("starting daily run (target=%s)", target)

    try:
        if target == "dev":
            init()

        failed_sources = [] if args.skip_ingest else step_ingest()

        run_dbt("run", "--select", "staging", "intermediate")
        # The one hard gate. Everything after this trusts that url_hash is unique.
        run_dbt("test", "--select", "int_items_dedup")

        unscored = 0 if args.skip_enrich else step_enrich()

        run_dbt("run", "--select", "marts")

        sent = step_deliver(failed_sources, unscored, args.dry_run)
        log.info(
            "done in %.0fs · delivered=%s", time.monotonic() - started, sent
        )
        return 0

    except StepFailed as exc:
        log.error("PIPELINE STOPPED: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        log.exception("unexpected failure: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
