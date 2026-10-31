"""Run a pinned private task without exposing its configuration or output."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import zipfile


def main():
    if not all(os.environ.get(key) for key in ("ACCESS", "STORE", "DATA", "BUNDLE_SHA")):
        print("Configuration required.", flush=True)
        return 1
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", os.environ["STORE"]):
        print("Invalid configuration.", flush=True)
        return 1
    root = Path(os.environ["RUNNER_TEMP"]) / "task"
    root.mkdir(mode=0o700)
    logfile = root / "details.txt"
    env = dict(os.environ, GH_TOKEN=os.environ["ACCESS"], GH_REPO=os.environ["STORE"], TASK_ROOT=str(root))
    tag = "run-" + os.environ["GITHUB_RUN_ID"]
    store_verified = False
    phase = "access"
    with logfile.open("wb", buffering=0) as log:
        def run(*args, capture=False, check=True):
            for attempt in range(3 if check else 1):
                result = subprocess.run(args, env=env, stdout=subprocess.PIPE if capture else log,
                                        stderr=log, check=False)
                if not check or result.returncode == 0:
                    return result
                if attempt < 2:
                    time.sleep(2 ** attempt)
            result.check_returncode()

        def publish():
            try:
                visibility = run("gh", "api", "repos/" + env["STORE"], "--jq", ".private", capture=True)
                if visibility.stdout.strip() != b"true":
                    return
                snapshot = root / "snapshot" / "details.txt"
                snapshot.parent.mkdir(exist_ok=True)
                shutil.copyfile(logfile, snapshot)
                run("gh", "release", "upload", tag, str(snapshot), "--clobber")
            except Exception:
                print("Private update deferred.", flush=True)

        try:
            result = run("gh", "api", "repos/" + env["STORE"], "--jq", ".private", capture=True)
            if result.stdout.strip() != b"true":
                raise RuntimeError("Private storage required")
            store_verified = True
            phase = "storage"
            exists = run("gh", "release", "view", tag, capture=True, check=False)
            if exists.returncode:
                run("gh", "release", "create", tag, "--title", "run", "--notes", "Private task state")
            phase = "configuration"
            run("gh", "release", "download", "config", "--pattern", "bundle.zip", "--dir", str(root))
            package = root / "bundle.zip"
            if hashlib.sha256(package.read_bytes()).hexdigest() != env["BUNDLE_SHA"]:
                raise RuntimeError("Configuration integrity check failed")
            with zipfile.ZipFile(package) as archive:
                for entry in archive.infolist():
                    target = (root / entry.filename).resolve()
                    if not target.is_relative_to(root.resolve()) or entry.filename.startswith(("/", "\\")):
                        raise RuntimeError("Invalid archive entry")
                archive.extractall(root)
            phase = "execution"
            child = subprocess.Popen([sys.executable, str(root / "worker.py")], cwd=root,
                                     env=env, stdout=log, stderr=log)
            last_publish = time.monotonic()
            while child.poll() is None:
                print("Working.", flush=True)
                time.sleep(45)
                if time.monotonic() - last_publish >= 300:
                    publish()
                    last_publish = time.monotonic()
            publish()
            if child.returncode == 75:
                print("Saved. Ready to continue.", flush=True)
                return 0
            if child.returncode:
                print("Stopped. Details are private.", flush=True)
                return 1
            print("Complete. Output is private.", flush=True)
            return 0
        except Exception:
            if store_verified:
                publish()
            if phase == "access":
                errors = logfile.read_text(errors="replace").lower()
                if "http 401" in errors or "bad credentials" in errors:
                    print("Access authentication failed.", flush=True)
                    return 1
                if "http 403" in errors:
                    print("Access permission denied.", flush=True)
                    return 1
            print(f"Stopped during {phase}. Check private configuration or details.", flush=True)
            return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Stopped.", flush=True)
        raise SystemExit(1)
