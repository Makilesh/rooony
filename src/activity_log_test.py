import time
import json
import win32gui
import win32process
import psutil
from datetime import datetime

LOG_FILE = "activity_log_test.jsonl"
INTERVAL_SECONDS = 5
MAX_ITERATIONS = 10  # stops automatically after ~50s for testing


def capture_once():
    hwnd = win32gui.GetForegroundWindow()
    title = win32gui.GetWindowText(hwnd)
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    proc_name = psutil.Process(pid).name()

    entry = {
        "timestamp": datetime.now().isoformat(),
        "window": title,
        "process": proc_name,
    }

    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")

    return entry


def main():
    print(f"Logging to {LOG_FILE} every {INTERVAL_SECONDS}s, for {MAX_ITERATIONS} iterations.")
    print("Press Ctrl+C to stop early.\n")

    try:
        for i in range(MAX_ITERATIONS):
            entry = capture_once()
            print(f"[{i + 1}/{MAX_ITERATIONS}] {entry['timestamp']} | {entry['process']} | {entry['window']}")
            time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopped by user.")

    print("Done.")


if __name__ == "__main__":
    main()
