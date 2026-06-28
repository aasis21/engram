#!/usr/bin/env python3
"""
Engram cross-platform installer.

Installs Engram from a local checkout, runs an initial index, and registers a
background scheduler that re-indexes incrementally:

  * macOS    -> a launchd LaunchAgent (com.aasis21.engram), runs every N minutes.
  * Windows  -> a hidden Scheduled Task ("Engram Indexer"), runs every N minutes.
  * Linux    -> install + initial index only (use cron/systemd manually).

Usage:
    python install.py                 # install + index + register scheduler
    python install.py --interval 5    # run every 5 minutes
    python install.py --no-schedule   # install + index, no background task
    python install.py --no-index      # register task, skip first index
    python install.py --uninstall     # remove scheduler (keep data)
    python install.py --uninstall --remove-data   # also delete db + install dir

Zero third-party dependencies (stdlib only), mirroring engram.py.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TASK_NAME = "Engram Indexer"
LAUNCHD_LABEL = "com.aasis21.engram"
FILES = ["engram.py", "config.json", "README.md"]


def install_dir() -> str:
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Engram")
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Engram")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "engram")


def skill_dest() -> str:
    return os.path.join(os.path.expanduser("~"), ".copilot", "skills", "engram")


def db_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".copilot", "session-store-vscode-chat.db")


def step(msg):
    print(f"\n== {msg} ==")


def copy_files(dest):
    os.makedirs(dest, exist_ok=True)
    for f in FILES:
        src = os.path.join(HERE, f)
        if os.path.isfile(src):
            shutil.copy2(src, dest)
            print(f"copied  : {f}")
        elif f == "engram.py":
            sys.exit("required file missing next to installer: engram.py")


def install_skill():
    src = os.path.join(HERE, "skills", "engram")
    if not os.path.isdir(src):
        return
    dest = skill_dest()
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    print(f"skill   : engram -> {dest}")


def run_index(engram):
    print("running initial index (full on first run, fast incremental otherwise)...")
    rc = subprocess.call([sys.executable, engram, "index"])
    if rc != 0:
        print(f"warning: index exited with code {rc}")


# --------------------------------------------------------------------------- #
# macOS launchd
# --------------------------------------------------------------------------- #
def agents_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")


def plist_path() -> str:
    return os.path.join(agents_dir(), f"{LAUNCHD_LABEL}.plist")


def register_launchd(engram, interval):
    os.makedirs(agents_dir(), exist_ok=True)
    p = plist_path()
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>{engram}</string>
    <string>index</string>
  </array>
  <key>StartInterval</key><integer>{interval * 60}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>/tmp/engram.err.log</string>
  <key>StandardOutPath</key><string>/tmp/engram.out.log</string>
</dict>
</plist>
"""
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(plist)
    subprocess.call(["launchctl", "unload", p], stderr=subprocess.DEVNULL)
    subprocess.call(["launchctl", "load", p])
    print(f"launchd : {LAUNCHD_LABEL} every {interval} min -> {p}")


def remove_launchd():
    p = plist_path()
    if os.path.isfile(p):
        subprocess.call(["launchctl", "unload", p], stderr=subprocess.DEVNULL)
        os.remove(p)
        print(f"removed launchd agent: {p}")
    else:
        print("no launchd agent found.")


# --------------------------------------------------------------------------- #
# Windows scheduled task
# --------------------------------------------------------------------------- #
def register_task(engram, interval):
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.isfile(pyw) else sys.executable
    ps = (
        f"$a=New-ScheduledTaskAction -Execute '{exe}' -Argument '\"{engram}\" index';"
        f"$t=New-ScheduledTaskTrigger -Once -At (Get-Date);"
        f"$t.Repetition=(New-ScheduledTaskTrigger -Once -At '00:00' "
        f"-RepetitionInterval (New-TimeSpan -Minutes {interval})).Repetition;"
        f"$s=New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -MultipleInstances IgnoreNew;"
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s -Force;"
        f"Start-ScheduledTask -TaskName '{TASK_NAME}'"
    )
    subprocess.call(["powershell", "-NoProfile", "-Command", ps])
    print(f"task    : {TASK_NAME} every {interval} min")


def remove_task():
    subprocess.call(["powershell", "-NoProfile", "-Command",
                     f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false"])
    print("removed scheduled task (if present).")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Install/uninstall Engram (cross-platform).")
    ap.add_argument("--interval", type=int, default=10, help="minutes between runs")
    ap.add_argument("--no-schedule", action="store_true", help="skip scheduler registration")
    ap.add_argument("--no-index", action="store_true", help="skip initial index")
    ap.add_argument("--uninstall", action="store_true", help="remove scheduler")
    ap.add_argument("--remove-data", action="store_true", help="with --uninstall: delete db + files")
    args = ap.parse_args()

    if args.uninstall:
        step("Uninstalling")
        if sys.platform == "darwin":
            remove_launchd()
        elif os.name == "nt":
            remove_task()
        if args.remove_data:
            for path in (install_dir(), skill_dest(), db_path()):
                if os.path.isdir(path):
                    shutil.rmtree(path); print(f"removed {path}")
                elif os.path.isfile(path):
                    os.remove(path); print(f"removed {path}")
        print("\n== Done ==")
        return

    dest = install_dir()
    step("Installing files")
    copy_files(dest)
    install_skill()
    engram = os.path.join(dest, "engram.py")

    if not args.no_index:
        step("Initial index")
        run_index(engram)

    if not args.no_schedule:
        step("Scheduler")
        if sys.platform == "darwin":
            register_launchd(engram, args.interval)
        elif os.name == "nt":
            register_task(engram, args.interval)
        else:
            print("no native scheduler for this OS; set up cron/systemd manually.")

    step("Done")
    subprocess.call([sys.executable, engram, "status"])


if __name__ == "__main__":
    main()
