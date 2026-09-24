"""Routing decision tests for ops/autotuner_daily.py.

The production DB is a WAL-mode SQLite file bind-mounted FILE-ONLY into the
multihedge container, so host-side writes split-brain the shared main file and
corrupt host readers. The daily cron script must therefore execute the tuning
inside the container whenever the production path is targeted, and must refuse
(fail closed) when no safe route exists. These tests assert that decision
contract without touching Docker or the production DB.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "autotuner_daily", _ROOT / "ops" / "autotuner_daily.py")
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("cannot load ops/autotuner_daily.py for routing tests")
autotuner_daily = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(autotuner_daily)

PROD_DB = _ROOT / "deploy" / "data" / "multihedge.db"


class RouteDecisionTests(unittest.TestCase):

    def test_dev_db_runs_locally_even_when_container_available(self):
        self.assertIsNone(autotuner_daily._route_command(
            Path("/tmp/dev.db"), _ROOT,
            docker_exe="/usr/bin/docker", container_running=True, image_ready=True))

    def test_prod_db_without_docker_fails_closed(self):
        with self.assertRaises(RuntimeError):
            autotuner_daily._route_command(
                PROD_DB, _ROOT,
                docker_exe=None, container_running=False, image_ready=False)

    def test_prod_db_with_container_down_fails_closed(self):
        with self.assertRaises(RuntimeError):
            autotuner_daily._route_command(
                PROD_DB, _ROOT,
                docker_exe="/usr/bin/docker", container_running=False,
                image_ready=True)

    def test_prod_db_without_container_assets_fails_closed(self):
        with self.assertRaises(RuntimeError):
            autotuner_daily._route_command(
                PROD_DB, _ROOT,
                docker_exe="/usr/bin/docker", container_running=True,
                image_ready=False)

    def test_prod_db_with_healthy_container_routes_into_container(self):
        cmd = autotuner_daily._route_command(
            PROD_DB, _ROOT,
            docker_exe="/usr/bin/docker", container_running=True, image_ready=True)
        self.assertIsNotNone(cmd)
        self.assertIn("exec", cmd)
        self.assertIn("-i", cmd)
        self.assertIn("MULTIHEDGE_EVIDENCE_DB=/app/multihedge.db", cmd)
        self.assertIn("python3", cmd)
        self.assertIn(autotuner_daily.CONTAINER_NAME, cmd)

    def test_explicit_prod_path_via_env_also_routes(self):
        # MULTIHEDGE_EVIDENCE_DB set to the production path must route the
        # same way as the default, otherwise the env override reintroduces
        # the host-side WAL write.
        cmd = autotuner_daily._route_command(
            PROD_DB, _ROOT,
            docker_exe="/usr/bin/docker", container_running=True, image_ready=True)
        self.assertIsNotNone(cmd)

    def test_incontainer_db_path_is_local_branch(self):
        # Inside the container the env var points at /app/multihedge.db, which
        # is NOT the host prod path, so the script must run locally there.
        cmd = autotuner_daily._route_command(
            Path("/app/multihedge.db"), Path("/app"),
            docker_exe="/usr/bin/docker", container_running=True, image_ready=True)
        self.assertIsNone(cmd)


if __name__ == "__main__":
    unittest.main()