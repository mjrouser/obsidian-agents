#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

MEETING_SYNC_TIMES = (
    (8, 35),
    (9, 5),
    (9, 35),
    (10, 5),
    (10, 35),
    (11, 5),
    (11, 35),
    (12, 5),
    (12, 35),
    (13, 5),
    (13, 35),
    (14, 5),
    (14, 35),
    (15, 5),
    (15, 35),
    (16, 5),
    (16, 35),
    (17, 5),
    (17, 35),
    (18, 5),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render launchd plist files for the local automation jobs.")
    parser.add_argument("--output-dir", default="ops/launchd/rendered")
    parser.add_argument("--label-prefix", default="com.obsidian.agent")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = repo_root / "scripts"
    output_dir = (repo_root / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for label, script_path, schedule in build_jobs(label_prefix=args.label_prefix, scripts_dir=scripts_dir):
        plist_path = output_dir / f"{label}.plist"
        plist_path.write_text(
            render_plist(label=label, script_path=script_path, schedule=schedule),
            encoding="utf-8",
        )
        print(plist_path)
    return 0


def meeting_sync_start_calendar_interval() -> str:
    entries: list[str] = []
    for weekday in range(1, 6):
        for hour, minute in MEETING_SYNC_TIMES:
            entries.append(
                "<dict>"
                f"<key>Weekday</key><integer>{weekday}</integer>"
                f"<key>Hour</key><integer>{hour}</integer>"
                f"<key>Minute</key><integer>{minute}</integer>"
                "</dict>"
            )
    return "<array>" + "".join(entries) + "</array>"


def build_jobs(*, label_prefix: str, scripts_dir: Path) -> list[tuple[str, Path, dict[str, str]]]:
    return [
        (
            f"{label_prefix}.intake-watcher",
            scripts_dir / "run_intake_watcher.sh",
            {
                "RunAtLoad": "<true/>",
                "KeepAlive": "<true/>",
            },
        ),
        (
            f"{label_prefix}.meeting-sync",
            scripts_dir / "run_meeting_sync.sh",
            {
                "StartCalendarInterval": meeting_sync_start_calendar_interval(),
            },
        ),
        (
            f"{label_prefix}.weekly-briefing",
            scripts_dir / "run_weekly_briefing.sh",
            {
                "StartCalendarInterval": (
                    "<dict><key>Weekday</key><integer>1</integer>"
                    "<key>Hour</key><integer>9</integer>"
                    "<key>Minute</key><integer>0</integer></dict>"
                ),
            },
        ),
        (
            f"{label_prefix}.weekly-wrap",
            scripts_dir / "run_weekly_wrap.sh",
            {
                "StartCalendarInterval": (
                    "<dict><key>Weekday</key><integer>5</integer>"
                    "<key>Hour</key><integer>12</integer>"
                    "<key>Minute</key><integer>15</integer></dict>"
                ),
            },
        ),
        (
            f"{label_prefix}.web-clipper",
            scripts_dir / "run_web_clipper_server.sh",
            {
                "RunAtLoad": "<true/>",
                "KeepAlive": "<true/>",
            },
        ),
    ]


def render_plist(*, label: str, script_path: Path, schedule: dict[str, str]) -> str:
    schedule_lines = "\n".join(f"  <key>{key}</key>\n  {value}" for key, value in schedule.items())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"  <key>Label</key>\n  <string>{label}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        "    <string>/bin/bash</string>\n"
        f"    <string>{script_path}</string>\n"
        "  </array>\n"
        f"{schedule_lines}\n"
        "</dict>\n"
        "</plist>\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
