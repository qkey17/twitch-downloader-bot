"""Synthetic, offline full-worker benchmark. One fresh process per size.

Run: python benchmarks/benchmark_streaming.py --sizes 10000 100000 250000
Only this benchmark's temporary files are removed. No credentials/API calls.
"""
import argparse
import asyncio
import json
import logging
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src import html_publisher
from src.chat_spool import ChatSpool
from src.resource_usage import memory_usage
from src.workers import download_worker, worker_html


async def measure(count, directory):
    baseline = memory_usage()
    sampled_peak = baseline["rss_bytes"] or 0
    finished = threading.Event()

    def sample():
        nonlocal sampled_peak
        while not finished.wait(0.02):
            sampled_peak = max(sampled_peak, memory_usage()["rss_bytes"] or 0)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    timing = {"data_generation_seconds": 0.0, "ingestion_seconds": 0.0,
              "html_stream_seconds": 0.0, "publication_total_seconds": 0.0,
              "spool_bytes": 0, "max_batch_messages": 0, "max_batch_json_bytes": 0}
    started = time.perf_counter()
    async def progress(*args, **kwargs):
        pass  # Do not retain per-progress mock call history in a memory benchmark.

    async def source(*args, **kwargs):
        ingest_start = time.perf_counter()
        for i in range(count):
            tick = time.perf_counter()
            text = f'message {i}: Привет 🎮 <tag> & \\ " Kappa '
            if i % 17 == 0:
                text += "длинное сообщение ✨ " * 100
            if i % 29 == 0:
                text += "</SCRIPT><script>ignored as text</script>"
            fragments = [{"text": text}, {"text": "Kappa", "emoteID": "25", "emoteName": "Kappa"}]
            comment = (i / 3, "2026-01-01T00:00:00Z", f"User_{i % max(2, count // 2)}", text,
                       fragments, "#abcdef", [{"set_id": "subscriber", "version": "1"}])
            timing["data_generation_seconds"] += time.perf_counter() - tick
            if i % 1000 == 0:
                memory = memory_usage()
                if (memory["rss_bytes"] or 0) > 700 * 1024 ** 2:
                    raise RuntimeError("Benchmark stopped above 700 MiB RSS")
                if shutil.disk_usage(directory).free < 1024 ** 3:
                    raise RuntimeError("Benchmark stopped below 1 GiB free disk")
            yield comment
        timing["ingestion_seconds"] = time.perf_counter() - ingest_start

    original_append, original_write = ChatSpool.append_batch, worker_html.write_viewer_html
    original_publish, original_close = html_publisher._write_html_atomically, ChatSpool.close

    def append(spool, batch, cancel_check=None):
        timing["max_batch_messages"] = max(timing["max_batch_messages"], len(batch))
        timing["max_batch_json_bytes"] = max(timing["max_batch_json_bytes"], sum(len(p.encode("utf-8")) for _, p in batch))
        return original_append(spool, batch, cancel_check)

    def close(spool):
        if spool.directory.exists():
            timing["spool_bytes"] = max(timing["spool_bytes"], spool.size_bytes())
        return original_close(spool)

    def write(*args, **kwargs):
        tick = time.perf_counter()
        try:
            return original_write(*args, **kwargs)
        finally:
            timing["html_stream_seconds"] += time.perf_counter() - tick

    def publish(*args, **kwargs):
        tick = time.perf_counter()
        try:
            return original_publish(*args, **kwargs)
        finally:
            timing["publication_total_seconds"] += time.perf_counter() - tick

    metadata = SimpleNamespace(title="Synthetic VOD", channel="streamer", length_seconds=count // 3,
                               channel_id="benchmark", created_at="2026-01-01T00:00:00Z",
                               thumbnail_preview_url="https://cdn.invalid/preview",
                               thumbnail_full_url="https://cdn.invalid/full")
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"aiohttp": object()}), user_data={})
    try:
        with ExitStack() as stack:
            for module, name, value in (
                (download_worker, "OUT_DIR", str(directory / "out")),
                (download_worker, "SPOOL_DIR", str(directory / "spools")),
                (download_worker, "get_client_id", lambda: "offline-benchmark"),
                (download_worker, "fetch_vod_meta", AsyncMock(return_value=metadata)),
                (download_worker, "gql_fetch_comments", source),
                (download_worker, "make_progress_updater", lambda *args: progress),
                (worker_html, "HTML_PUBLISH_MODE", "local"),
                (html_publisher, "OUT_DIR", str(directory / "out")),
                (ChatSpool, "append_batch", append), (ChatSpool, "close", close),
                (worker_html, "write_viewer_html", write),
                (html_publisher, "_write_html_atomically", publish),
            ):
                stack.enter_context(patch.object(module, name, value))
            for name in ("fetch_7tv_emote_map", "fetch_bttv_emote_map", "fetch_ffz_emote_map",
                         "fetch_twitch_global_emote_map", "fetch_twitch_global_badge_map",
                         "fetch_twitch_channel_badge_map"):
                value = {"subscriber/1": "https://cdn.invalid/badge"} if "badge" in name else {"Kappa": "https://cdn.invalid/kappa"}
                stack.enter_context(patch.object(worker_html, name, AsyncMock(return_value=value)))
            stack.enter_context(patch.object(worker_html, "fetch_twitch_channel_emote_maps",
                                            AsyncMock(return_value=({"Kappa": "https://cdn.invalid/kappa"}, {"25": "https://cdn.invalid/kappa"}))))
            _, stats, _, ref = await download_worker.download_and_send(context, 1, 2, "vod", "synthetic", "html_online")
        html_path = directory / "out" / ref.removeprefix("local:")
        if stats["messages"] != count or stats["unique_users"] != min(count, max(2, count // 2)):
            raise AssertionError("Benchmark lost messages or unique users")
        if tuple((directory / "spools").iterdir()):
            raise AssertionError("Temporary chat storage was not cleaned")
        end_memory = memory_usage()
        sampled_peak = max(sampled_peak, end_memory["rss_bytes"] or 0)
        return {
            "messages": count, "unique_users": stats["unique_users"],
            "python": platform.python_version(), "platform": platform.platform(),
            "baseline_rss_bytes": baseline["rss_bytes"], "final_rss_bytes": end_memory["rss_bytes"],
            "sampled_peak_rss_bytes": sampled_peak,
            "additional_sampled_rss_bytes": sampled_peak - (baseline["rss_bytes"] or 0),
            "process_lifetime_peak_rss_bytes": end_memory["process_peak_rss_bytes"],
            "swap_bytes": end_memory["swap_bytes"], "private_bytes": end_memory.get("private_bytes"),
            "html_bytes": html_path.stat().st_size, "elapsed_seconds": time.perf_counter() - started,
            **timing,
            "publication_overhead_seconds": max(0, timing["publication_total_seconds"] - timing["html_stream_seconds"]),
        }
    finally:
        finished.set()
        sampler.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[10000, 100000, 250000])
    parser.add_argument("--worker", type=int)
    parser.add_argument("--output", type=Path, help="Save small JSON measurement records; must not exist")
    parser.add_argument("--work-dir", type=Path, default=PROJECT_ROOT / "_work")
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error("Output already exists; choose a new result file")
    logging.disable(logging.CRITICAL)
    if args.worker:
        if not 1 <= args.worker <= 250000:
            parser.error("Worker size must be 1..250000")
        args.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="tdb-benchmark-", dir=args.work_dir) as directory:
            print(json.dumps(asyncio.run(measure(args.worker, Path(directory)))), flush=True)
    else:
        records = []
        for size in args.sizes:
            result = subprocess.run(
                [sys.executable, "-B", str(Path(__file__).resolve()), "--worker", str(size), "--work-dir", str(args.work_dir)],
                capture_output=True, text=True, timeout=240, check=True,
            )
            print(result.stdout.strip(), flush=True)
            records.append(json.loads(result.stdout))
        if args.output:
            with args.output.open("x", encoding="utf-8") as output:
                json.dump(records, output, indent=2)


if __name__ == "__main__":
    main()
