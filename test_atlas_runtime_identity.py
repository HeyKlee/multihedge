"""Runtime-identity checks for Project ATLAS.

The deployed image is built by `COPY *.py /app/`, so the container receives every root-level
python file present on the build host — including files that are never committed. This
means "the container runs the repo" is an assumption, not a fact, unless it is checked.

These tests assert the assumption. They are read-only: no container is restarted, no
database is written, and no trading cycle is run.
"""
import json
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
CONTAINER = "multihedge"


def sh(cmd: list[str], timeout: int = 90) -> tuple[int, str, str]:
    p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def container_running() -> bool:
    rc, out, _ = sh(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER])
    return rc == 0 and out.strip() == "true"


def app_python_files() -> set[str]:
    rc, out, _ = sh(["docker", "exec", CONTAINER, "sh", "-c", "ls /app/*.py 2>/dev/null"])
    if rc != 0:
        return set()
    return {line.strip().split("/")[-1] for line in out.split() if line.strip().endswith(".py")}


def tracked_python_files() -> set[str]:
    rc, out, _ = sh(["git", "ls-files", "*.py"])
    return {p.split("/")[-1] for p in out.split() if p.endswith(".py")}


class ContainerMatchesRepository(unittest.TestCase):
    """Every python file in the image must exist in git at the same content."""

    @unittest.skipUnless(container_running(), "multihedge container not running")
    def test_no_untracked_source_in_image(self):
        untracked = sorted(app_python_files() - tracked_python_files())
        if untracked:
            self.fail(
                "The running image contains python files that are not git-tracked, so the "
                "container cannot be proven to match the repository:\n  "
                + "\n  ".join(untracked)
                + "\nEither commit them or remove them from the build context. A rebuild "
                  "from a clean checkout will otherwise silently drop them."
            )

    @unittest.skipUnless(container_running(), "multihedge container not running")
    def test_image_python_content_matches_working_tree(self):
        """Same filename is not proof of same content. Compare hashes for root modules."""
        rc, out, _ = sh([
            "docker", "exec", CONTAINER,
            "sh", "-c", "cd /app && md5sum *.py 2>/dev/null | sort",
        ])
        if rc != 0:
            self.skipTest("md5sum unavailable in container")
        container = {}
        for line in out.split("\n"):
            parts = line.split()
            if len(parts) == 2 and parts[1].endswith(".py"):
                container[parts[1]] = parts[0]

        mismatched = []
        for name, digest in container.items():
            local = REPO / name
            if not local.is_file():
                continue  # covered by the untracked test
            local_digest = subprocess.run(
                ["md5sum", str(local)], capture_output=True, text=True, check=False
            ).stdout.split()[0]
            if local_digest != digest:
                mismatched.append(f"{name}: container={digest} repo={local_digest}")
        self.assertEqual(
            mismatched, [],
            "Image content differs from the repository for the same filename; a deploy "
            "claim cannot be verified from the repo alone:\n  " + "\n  ".join(mismatched),
        )


class BuildContextIsReproducible(unittest.TestCase):
    def test_dockerfile_copies_a_deterministic_file_set(self):
        dockerfile = (REPO / "deploy" / "Dockerfile").read_text(encoding="utf-8", errors="replace")
        self.assertIn(
            "COPY *.py /app/", dockerfile,
            "Dockerfile no longer uses `COPY *.py /app/`; update this test and the ATLAS "
            "runtime-identity contract to match the new build rule.",
        )
        # `COPY *.py` is glob-based, so anything untracked on the build host ships.
        # The guard above (no untracked source) is what makes that safe.
        self.assertTrue(
            (REPO / ".dockerignore").is_file(),
            "A glob-based COPY requires a .dockerignore to keep stray host files "
            "(secrets, scratch scripts, untracked modules) out of the image.",
        )


class RuntimeManifestContract(unittest.TestCase):
    def test_manifest_endpoint_is_planned_but_absent(self):
        """Plan §13 requires GET /api/system-manifest so runtime state is provable.

        This test records the gap. It flips to a requirement once the endpoint lands.
        """
        rc, out, _ = sh([
            "docker", "exec", CONTAINER, "python3", "-c",
            "import re,sys; s=open('/app/mh_dash.py',encoding='utf-8',errors='replace').read();"
            "print('FOUND' if re.search(r'[\"\\']/api/system-manifest[\"\\']', s) else 'ABSENT')",
        ])
        if rc != 0:
            self.skipTest("could not inspect mh_dash.py in container")
        if "ABSENT" in out:
            self.fail(
                "GET /api/system-manifest does not exist (plan §13). Without it there is no "
                "way to prove which git SHA, config hash, schema version and policy version "
                "are actually running — which is how FINDING 004 stayed invisible."
            )


if __name__ == "__main__":
    unittest.main()
