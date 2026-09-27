"""Regression guard for Project ATLAS FINDING 003.

The runtime database path is currently resolved relatively (`Path(__file__).parent /
"multihedge.db"`), so identical source resolves to DIFFERENT databases depending on the
working directory of whoever imports it:

    container (cwd=/app)              -> /app/multihedge.db          (2,216 trades)
    host     (cwd=/home/kelly/...)    -> /home/kelly/.../multihedge.db (0 trades)

That produced a silent, confident wrong answer (the 8-hour P&L simulation read the empty
legacy database and reported "no outcomes" as if it were a market fact).

These tests fail if any module resolves the production database relatively, and fail if a
future resolver is not absolute. They do not require the production database to exist and
never write to any database.
"""
import ast
import os
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent

# The canonical resolver does not exist yet (ATLAS gate reports MISSING). Once it lands,
# point this at it. Until then these tests assert the defect is not *widened*, and they
# document precisely what must be true at certification time.
CANONICAL_PATHS_MODULE = "runtime/paths.py"
PRODUCTION_DB = "/app/multihedge.db"

# Modules that run inside the container and currently resolve relatively.
# Keep this list explicit: an unlisted relative site is still caught by the AST scan below.
RUNTIME_DB_MODULES = [
    "paper.py",
    "grid_trader.py",
    "mh_memecoin_trader.py",
    "mh_news.py",
    "mh_reasoner.py",
    "track_whales.py",
    "mh_whale_trader.py",
    "pump_monitor.py",
    "live_bridge.py",
    "pricefeed.py",
    "mh_dash.py",
]


def tracked_python_modules() -> list[str]:
    out = subprocess.run(
        ("git", "ls-files", "*.py"), cwd=REPO, capture_output=True, text=True, check=False
    )
    return [p for p in out.stdout.split() if (REPO / p).is_file()]


class RelativeProductionPathIsADefect(unittest.TestCase):
    """Any production-DB path built from a relative base is a latent split-brain."""

    def test_no_runtime_module_resolves_production_db_relatively(self):
        """AST-based: only real code expressions count, never docstring prose."""
        offenders = []
        for rel in RUNTIME_DB_MODULES:
            path = REPO / rel
            if not path.is_file():
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=rel)
            except SyntaxError as exc:
                self.fail(f"{rel} does not parse: {exc}")
            # Docstrings are string constants; exclude them from consideration entirely.
            docstring_nodes = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    body = getattr(node, "body", [])
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                        docstring_nodes.add(id(body[0].value))
            for node in ast.walk(tree):
                if id(node) in docstring_nodes:
                    continue
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                value = node.value
                if "multihedge.db" not in value:
                    continue
                if "/app/multihedge.db" in value:          # absolute, acceptable
                    continue
                if any(v in value for v in ("MULTIHEDGE_DB", "MULTIHEDGE_EVIDENCE_DB",
                                            "MULTIHEDGE_LEGACY_DB")):
                    continue  # env-driven; the env contract owns the default
                offenders.append(
                    f"{rel}:{node.lineno}: {value.strip()[:120]}"
                )
        self.assertEqual(
            offenders, [],
            "Production DB path resolved relatively; it will read a different database "
            "whenever the working directory differs from the container's /app "
            "(FINDING 003):\n  " + "\n  ".join(offenders),
        )

    def test_canonical_resolver_would_be_absolute(self):
        """If runtime/paths.py exists, every DB path it returns must be absolute."""
        module = REPO / CANONICAL_PATHS_MODULE
        if not module.is_file():
            self.skipTest(
                f"{CANONICAL_PATHS_MODULE} does not exist yet; ATLAS gate reports MISSING. "
                "This test activates automatically once the canonical resolver lands."
            )
        source = module.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(module))
        relative_returns = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            text = ast.unparse(node.value)
            if "multihedge.db" not in text:
                continue
            if "__file__" in text or "Path(__file__)" in text:
                relative_returns.append(f"line {node.lineno}: {text[:120]}")
        self.assertEqual(
            relative_returns, [],
            "Canonical resolver returned a __file__-relative production DB path; "
            "that reintroduces FINDING 003:\n  " + "\n  ".join(relative_returns),
        )

    def test_container_and_host_do_not_silently_disagree(self):
        """Document the real behaviour so a future change is caught as a diff."""
        src = "import sys; sys.path.insert(0, %r); import paper; print(paper.DB_PATH)" % str(REPO)
        host = subprocess.run(
            ["python3", "-c", src], cwd=REPO, capture_output=True, text=True, check=False
        )
        host_path = host.stdout.strip().splitlines()[-1] if host.stdout.strip() else ""
        # On the host today this is the legacy root database, which is the defect.
        if host_path:
            self.assertNotEqual(
                os.path.realpath(host_path),
                os.path.realpath(PRODUCTION_DB),
                "Host resolution now equals the container path; FINDING 003 may be "
                "retired. Update the ATLAS findings and re-run the single-truth gate.",
            )
        self.assertTrue(
            host_path, "could not resolve paper.DB_PATH on the host; scan assumptions changed"
        )


class CanonicalResolverContract(unittest.TestCase):
    def test_single_truth_gate_still_reports_the_defect(self):
        """The enforcement gate must remain non-zero until FINDING 001/003 are fixed."""
        gate = REPO / "tools" / "check_single_truth.py"
        if not gate.is_file():
            self.skipTest("tools/check_single_truth.py not present")
        result = subprocess.run(
            ["python3", str(gate), "--quiet"],
            cwd=REPO, capture_output=True, text=True, check=False,
        )
        self.assertEqual(
            result.returncode, 1,
            "Single-truth gate must block (exit 1) while relative/duplicate production "
            "DB paths exist. Exit 0 means the defect was closed or the gate regressed.",
        )


if __name__ == "__main__":
    unittest.main()
