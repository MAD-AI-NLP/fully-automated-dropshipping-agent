"""
scheduler.py
============
Enhanced with tqdm progress bars and detailed step logging.
"""

import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from tqdm import tqdm

from config_loader import get_layer_config
from layer1.pipeline import run_layer1
from layer2.pipeline import run_layer2

STATE_PATH = Path("layer2/output/publish_state.json")
WINNING_PRODUCTS_PATH = Path("layer1/output/winning_products.json")

LOOP_CHECK_INTERVAL_SECONDS = 600  # 10 minutes


# ── State helpers ────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"[Scheduler] ⚠️  {STATE_PATH} is corrupt/unreadable — treating as empty state.")
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def _load_winning_products() -> list:
    if not WINNING_PRODUCTS_PATH.exists():
        return []
    try:
        with open(WINNING_PRODUCTS_PATH) as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"[Scheduler] ⚠️  {WINNING_PRODUCTS_PATH} is corrupt/unreadable — treating as empty batch.")
        return []


def _adopt_or_reset_batch(products: list) -> dict:
    """Builds a fresh state dict pointed at the START of `products`."""
    return {
        "batch_created_at": datetime.now().isoformat(),
        "next_index": 0,
        "total_products": len(products),
        "last_run_at": None,
        "last_result": None,
    }


def _run_layer1_and_reset_state() -> dict:
    """Triggers a brand-new Layer 1 run with progress indication."""
    print("\n" + "=" * 70)
    print("🏭 LAYER 1: Market Intelligence Pipeline")
    print("=" * 70)
    print(f"📊 Current batch exhausted ({_load_state().get('total_products', 0)} products consumed)")
    print("🔄 Fetching fresh batch of winning products...\n")

    # Run Layer 1 with progress
    with tqdm(total=1, desc="🔍 Running Layer 1 Pipeline", unit="run") as pbar:
        run_layer1()
        pbar.update(1)

    products = _load_winning_products()
    state = _adopt_or_reset_batch(products)
    _save_state(state)

    print(f"\n✅ Layer 1 complete!")
    print(f"   📦 Found: {len(products)} winning products")
    print(f"   📁 Saved to: {WINNING_PRODUCTS_PATH}")
    print("=" * 70 + "\n")

    return state


def _is_due(state: dict, frequency_hours: float, empty_retry_hours: float) -> bool:
    last_run_at = state.get("last_run_at")
    if not last_run_at:
        return True
    last_dt = datetime.fromisoformat(last_run_at)
    wait_hours = empty_retry_hours if state.get("last_result") == "empty" else frequency_hours
    return datetime.now() >= last_dt + timedelta(hours=wait_hours)


def _next_due_at(state: dict, frequency_hours: float, empty_retry_hours: float) -> datetime:
    last_run_at = state.get("last_run_at")
    if not last_run_at:
        return datetime.now()
    wait_hours = empty_retry_hours if state.get("last_result") == "empty" else frequency_hours
    return datetime.fromisoformat(last_run_at) + timedelta(hours=wait_hours)


def print_status() -> None:
    """Read-only view of where things stand — no side effects."""
    layer2_cfg = get_layer_config("layer2")
    batch_size = layer2_cfg.get("products_per_publish_run", 5)
    frequency_hours = layer2_cfg.get("publish_frequency_hours", 72)
    empty_retry_hours = layer2_cfg.get("empty_batch_retry_hours", 6)
    state = _load_state()

    print("\n" + "=" * 60)
    print("📋 SCHEDULER STATUS")
    print("=" * 60)
    print(f"  Products per tick:     {batch_size}")
    print(f"  Frequency (hours):     {frequency_hours}")
    print(f"  Empty-batch retry (h): {empty_retry_hours}")

    if not state:
        print("  No scheduler state yet — first run will bootstrap it.")
        return

    total = state.get("total_products", 0)
    idx = state.get("next_index", 0)
    remaining = max(total - idx, 0)

    print("\n  📦 Batch Status:")
    print(f"    Created at:          {state.get('batch_created_at')}")
    print(f"    Total products:      {total}")
    print(f"    Already published:   {idx}")
    print(f"    Remaining:           {remaining}")

    if total > 0:
        pct = (idx / total * 100) if total > 0 else 0
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"    Progress:            [{bar}] {pct:.1f}%")

    print(f"\n  ⏰ Timing:")
    print(f"    Last tick at:        {state.get('last_run_at') or 'never'}")
    print(f"    Last tick result:    {state.get('last_result') or 'n/a'}")

    if state.get("last_run_at"):
        next_due = _next_due_at(state, frequency_hours, empty_retry_hours)
        wait_seconds = max(0, (next_due - datetime.now()).total_seconds())
        wait_minutes = wait_seconds / 60
        print(f"    Next publish due:    {next_due.isoformat()}")
        if wait_seconds > 0:
            print(f"    Time remaining:      {wait_minutes:.1f} minutes")
    print("=" * 60 + "\n")


def reset_state() -> None:
    if STATE_PATH.exists():
        STATE_PATH.unlink()
        print(f"[Scheduler] 🗑️  Deleted {STATE_PATH}. Next tick starts fresh.")
    else:
        print(f"[Scheduler] ℹ️  No state file at {STATE_PATH} — nothing to reset.")


# ── Core tick with enhanced logging ─────────────────────────────────────────

def run_tick(force: bool = False) -> None:
    """
    A single scheduler tick with detailed progress logging.
    """
    layer2_cfg = get_layer_config("layer2")
    batch_size = layer2_cfg.get("products_per_publish_run", 5)
    frequency_hours = layer2_cfg.get("publish_frequency_hours", 72)
    empty_retry_hours = layer2_cfg.get("empty_batch_retry_hours", 6)

    # ── Load state ──────────────────────────────────────────────────────────
    state = _load_state()

    if not state:
        print("🔄 No scheduler state found — initializing with existing winning products...")
        state = _adopt_or_reset_batch(_load_winning_products())
        _save_state(state)
        print(f"   📦 Found {state['total_products']} products in current batch")

    # ── Check if due ────────────────────────────────────────────────────────
    if not force and not _is_due(state, frequency_hours, empty_retry_hours):
        next_due = _next_due_at(state, frequency_hours, empty_retry_hours)
        wait_seconds = max(0, (next_due - datetime.now()).total_seconds())

        reason = " (retrying soon — last attempt found 0 products)" if state.get("last_result") == "empty" else ""
        print(f"\n⏳ Waiting for next scheduled tick:")
        print(f"   Next due at:     {next_due.isoformat()}")
        print(f"   Time remaining:  {wait_seconds / 60:.1f} minutes")
        print(f"   Status:          {state.get('last_result', 'waiting')}{reason}")
        return

    # ── Ensure we have an unexhausted batch ──────────────────────────────
    total = state.get("total_products", 0)
    idx = state.get("next_index", 0)
    remaining = max(total - idx, 0)

    print(f"\n📊 Batch status before tick:")
    print(f"   Total products:    {total}")
    print(f"   Already published: {idx}")
    print(f"   Remaining:         {remaining}")

    if remaining <= 0:
        state = _run_layer1_and_reset_state()
        total = state.get("total_products", 0)
        remaining = total

    products = _load_winning_products()

    if not products:
        print("\n⚠️  Layer 1 produced 0 winning products — nothing to publish.")
        print(f"   Will retry in ~{empty_retry_hours}h instead of full {frequency_hours}h cycle.")
        state["last_run_at"] = datetime.now().isoformat()
        state["last_result"] = "empty"
        _save_state(state)
        return

    # ── Prepare batch ──────────────────────────────────────────────────────
    next_index = state.get("next_index", 0)
    batch = products[next_index: next_index + batch_size]

    if not batch:
        state = _run_layer1_and_reset_state()
        products = _load_winning_products()
        batch = products[:batch_size]

    # ── Display batch info ────────────────────────────────────────────────
    batch_start = next_index + 1
    batch_end = next_index + len(batch)
    print(f"\n📦 Publishing batch #{batch_start}-{batch_end} of {len(products)} total products")
    print(f"   Batch size:        {len(batch)} products")
    print(f"   Remaining after:   {len(products) - batch_end} products")
    print("-" * 60)

    # ── Run Layer 2 ────────────────────────────────────────────────────────
    l2_state = run_layer2(winning_products=batch)

    # ── Results ────────────────────────────────────────────────────────────
    published = len(l2_state.get("published_listings", []))
    failed = len(l2_state.get("failed_listings", []))

    print("\n" + "=" * 60)
    print("📊 TICK COMPLETE")
    print("=" * 60)
    print(f"  ✅ Published:       {published} products")
    print(f"  ❌ Failed:          {failed} products")
    print(f"  📦 Batch progress:  {batch_end}/{len(products)}")

    # Progress bar
    pct = (batch_end / len(products) * 100) if len(products) > 0 else 0
    bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
    print(f"  Progress:          [{bar}] {pct:.1f}%")
    print("=" * 60)

    # ── Update state ──────────────────────────────────────────────────────
    state["next_index"] = next_index + len(batch)
    state["total_products"] = len(products)
    state["last_run_at"] = datetime.now().isoformat()
    state["last_result"] = "published"
    _save_state(state)

    remaining_after = len(products) - state["next_index"]
    if remaining_after > 0:
        print(f"\n📊 {remaining_after} product(s) remaining in current batch.")
        print(
            f"   Next tick will publish positions {state['next_index'] + 1}-{state['next_index'] + min(batch_size, remaining_after)}")
    else:
        print(f"\n📊 Batch fully consumed! Next tick will trigger Layer 1 for a fresh batch.")


# ── Entry point with enhanced loop logging ─────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scheduler that runs Layer 1 (batch discovery) + Layer 2 "
                    "(metered publish, N products every F hours) automatically."
    )
    parser.add_argument("--once", action="store_true",
                        help="Run a single tick and exit. Use this from an external "
                             "cron/systemd timer (recommended).")
    parser.add_argument("--loop", action="store_true",
                        help="Run forever in-process, checking the clock every "
                             f"{LOOP_CHECK_INTERVAL_SECONDS}s. Use if you'd rather not "
                             "rely on cron/systemd.")
    parser.add_argument("--force", action="store_true",
                        help="Ignore the frequency check and publish immediately. "
                             "Intended for use with --once for manual/testing runs — "
                             "combining with --loop will publish on every poll.")
    parser.add_argument("--status", action="store_true",
                        help="Print current batch/queue status and exit. No side effects.")
    parser.add_argument("--reset", action="store_true",
                        help="Delete the scheduler's state file and exit. Use this to unstick "
                             "a scheduler waiting on a stale timestamp, or to force a clean restart.")
    args = parser.parse_args()

    if args.reset:
        reset_state()
        return

    if args.status:
        print_status()
        return

    if args.loop:
        print("\n" + "=" * 70)
        print("🔄 SCHEDULER STARTING IN LOOP MODE")
        print("=" * 70)
        print(f"  Check interval:     {LOOP_CHECK_INTERVAL_SECONDS}s (every 10 minutes)")
        print(f"  Publish frequency:  {get_layer_config('layer2').get('publish_frequency_hours', 72)} hours")
        print(f"  Products per tick:  {get_layer_config('layer2').get('products_per_publish_run', 5)}")
        print("=" * 70 + "\n")

        tick_count = 0
        while True:
            tick_count += 1
            print(f"\n{'=' * 70}")
            print(f"🔄 SCHEDULER TICK #{tick_count} — {datetime.now().isoformat()}")
            print(f"{'=' * 70}")

            try:
                run_tick(force=args.force)
            except Exception as e:
                print(f"\n❌ Tick failed with error: {e}")
                import traceback
                traceback.print_exc()

            # Countdown to next check
            print(f"\n⏰ Next check in {LOOP_CHECK_INTERVAL_SECONDS}s...")

            # Animated countdown
            for remaining in range(LOOP_CHECK_INTERVAL_SECONDS // 10, 0, -1):
                print(f"\r   {'⏳' * (remaining % 5)} {remaining * 10}s remaining", end="")
                time.sleep(10)
            print("\r   " + " " * 40 + "\r", end="")

    else:
        run_tick(force=args.force)


if __name__ == "__main__":
    main()