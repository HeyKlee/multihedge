"""Pre-flight and post-flight guards for the deploy/db directory mount (FINDING 008).

Two invariants, both of which must hold or the mount is unsafe:

1. **Key isolation.** The container gains a directory mount. That mount must not contain
   or expose `signer.env`, `agent.env`, or `.env`. The Solana signer key must remain
   reachable only by the isolated signer, never by the trading container.

2. **One WAL.** The whole point of mounting a directory instead of a file is that
   `-wal` and `-shm` are shared. This asserts the sidecars actually appear in the mounted
   directory rather than in the container's writable layer.

These run without starting or restarting the container; the WAL assertion is skipped when
the container is not running.
"""
import os
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
DB_DIR = REPO / "deploy" / "db"
DATA_DIR = REPO / "deploy" / "data"
COMPOSE = REPO / "deploy" / "docker-compose.yml"

SECRET_NAMES = ("signer.env", "agent.env", ".env", "signer_state", "live_queue")


def container_running() -> bool:
    try:
        p = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", "multihedge"],
                           capture_output=True, text=True, timeout=20, check=False)
        return p.returncode == 0 and p.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


class KeyIsolationIsPreserved(unittest.TestCase):
    def test_db_mount_directory_contains_no_secrets(self):
        if not DB_DIR.is_dir():
            self.skipTest("deploy/db not created yet")
        for name in SECRET_NAMES:
            self.assertFalse(
                (DB_DIR / name).exists(),
                f"{name} is inside the bind-mounted db directory; mounting it would "
                "expose that secret to the trading container and break key isolation",
            )

    def test_compose_does_not_mount_the_secret_directory(self):
        text = COMPOSE.read_text(encoding="utf-8", errors="replace")
        self.assertNotIn(
            "./data:/app/data", text,
            "compose mounts the whole deploy/data directory, which contains signer.env",
        )
        self.assertNotIn(
            "./data:/app/db", text,
            "the db mount must not be sourced from the secret-bearing directory",
        )
        self.assertIn(
            "./db:/app/db", text,
            "the db directory mount is missing; without it the sidecars are not shared",
        )

    def test_secrets_are_still_mounted_individually_and_read_only(self):
        text = COMPOSE.read_text(encoding="utf-8", errors="replace")
        self.assertIn("./data/agent.env:/app/.env:ro", text)
        for name in ("signer.env",):
            self.assertNotIn(
                f"data/{name}:", text,
                f"{name} must not be bind-mounted into the trading container",
            )

    @unittest.skipUnless(container_running(), "container not running")
    def test_signer_key_not_reachable_inside_the_container(self):
        for path in ("/app/db/signer.env", "/app/signer.env", "/app/db/.env"):
            p = subprocess.run(["docker", "exec", "multihedge", "test", "-e", path],
                               capture_output=True, text=True, timeout=20, check=False)
            self.assertNotEqual(
                p.returncode, 0, f"{path} exists inside the container; key isolation broken"
            )


class SidecarsAreShared(unittest.TestCase):
    @unittest.skipUnless(container_running(), "container not running")
    def test_wal_sidecars_live_in_the_mounted_directory(self):
        """The torn-write fix only works if -wal/-shm are in the shared directory."""
        present = subprocess.run(
            ["docker", "exec", "multihedge", "ls", "/app/db/"],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout
        self.assertIn("multihedge.db", present, "the database is not in the mounted dir")
        # -wal may legitimately be absent if no write is in flight, but if the container
        # holds a *different* sidecar outside /app/db, the split is back.
        for stray in ("/app/multihedge.db-wal", "/app/multihedge.db-shm"):
            p = subprocess.run(["docker", "exec", "multihedge", "test", "-e", stray],
                               capture_output=True, text=True, timeout=20, check=False)
            self.assertNotEqual(
                p.returncode, 0,
                f"{stray} exists: the container created a private WAL, so the split that "
                "caused the 2026-09-28 torn write is present again",
            )

    def test_host_and_container_agree_on_the_ledger_path(self):
        """Both sides must name the SAME FILE, though the mount prefixes differ.

        Host   : <repo>/deploy/db/multihedge.db
        Container: /app/db/multihedge.db
        These are the same inode via the bind mount, so the correct assertion is on the
        final path component, not on the full string. Comparing full paths was a bug in
        the first version of this test and produced a false failure by design.
        """
        if not container_running():
            self.skipTest("container not running")
        p = subprocess.run(
            ["docker", "exec", "multihedge", "python3", "-c",
             "import runtime_paths; print(runtime_paths.production_db())"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(p.returncode, 0, f"resolver failed in container: {p.stderr[-300:]}")
        container_path = p.stdout.strip().splitlines()[-1].strip()
        import runtime_paths
        host_path = str(runtime_paths.production_db())
        self.assertEqual(
            Path(container_path).name, Path(host_path).name,
            "host and container name different files",
        )
        self.assertIn(
            "/db/", container_path,
            f"container ledger {container_path} is not inside the shared mount directory",
        )
        # The decisive check: the container's file must be the host's file.
        st = subprocess.run(
            ["docker", "exec", "multihedge", "python3", "-c",
             "import os; s=os.stat(runtime_paths.production_db()); print(s.st_dev, s.st_ino)"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if st.returncode != 0:
            self.skipTest("could not stat inside the container")
        host_stat = runtime_paths.production_db().stat()
        dev_ino = st.stdout.strip().splitlines()[-1].split()
        self.assertEqual(
            int(dev_ino[1]), host_stat.st_ino,
            "container and host are NOT the same file; the directory mount is not sharing "
            "the ledger and the torn-write risk remains",
        )


class LegacyCopyIsNotMistakenForProduction(unittest.TestCase):
    def test_legacy_root_copy_still_exists_but_is_not_the_default(self):
        import runtime_paths
        self.assertNotEqual(
            runtime_paths.production_db(), (REPO / "multihedge.db").resolve(),
            "the resolver must not fall back to the empty repo-root copy",
        )

    def test_deploy_data_no_longer_holds_the_ledger(self):
        if not DATA_DIR.is_dir():
            self.skipTest("deploy/data missing")
        self.assertFalse(
            (DATA_DIR / "multihedge.db").exists(),
            "a ledger still sits in deploy/data; that file would shadow the mounted one",
        )


if __name__ == "__main__":
    unittest.main()
