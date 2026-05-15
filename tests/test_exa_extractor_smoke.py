"""
Smoke tests for phase1_extract/exa_extractor.py — CLI parsing, tier resolution,
and run() guard rails (no live Exa calls).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
EXA_SCRIPT = ROOT / "phase1_extract" / "exa_extractor.py"

_exa_module = None


def _exa():
    """Load exa_extractor once (script layout: not a package)."""
    global _exa_module
    if _exa_module is not None:
        return _exa_module
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("exa_extractor_smoke", EXA_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _exa_module = mod
    return mod


class TestResolveTiers(unittest.TestCase):
    def test_none_defaults_noida(self):
        m = _exa()
        self.assertEqual(m.resolve_tiers(None), ["noida"])

    def test_empty_defaults_noida(self):
        m = _exa()
        self.assertEqual(m.resolve_tiers([]), ["noida"])

    def test_all_expands_cone(self):
        m = _exa()
        self.assertEqual(m.resolve_tiers(["all"]), list(m.GEO_CONE))

    def test_mixed_all_wins(self):
        m = _exa()
        self.assertEqual(m.resolve_tiers(["noida", "all"]), list(m.GEO_CONE))

    def test_subset_order_preserved(self):
        m = _exa()
        self.assertEqual(m.resolve_tiers(["delhi", "noida"]), ["delhi", "noida"])

    def test_invalid_dropped_fallback_noida(self):
        m = _exa()
        self.assertEqual(m.resolve_tiers(["not-a-tier"]), ["noida"])


class TestArgparseModesAndTiers(unittest.TestCase):
    def test_each_mode_flag(self):
        m = _exa()
        for mode in sorted(m.VALID_MODES):
            with self.subTest(mode=mode):
                args = m._parse_args(["--mode", mode])
                self.assertEqual(args.modes, [mode])

    def test_repeated_mode_flags(self):
        m = _exa()
        args = m._parse_args(["--mode", "similar", "--mode", "search"])
        self.assertEqual(args.modes, ["similar", "search"])

    def test_each_tier_flag(self):
        m = _exa()
        for tier in [*m.GEO_CONE, "all"]:
            with self.subTest(tier=tier):
                args = m._parse_args(["--mode", "agent", "--tier", tier])
                self.assertEqual(args.tiers, [tier])

    def test_multi_tier(self):
        m = _exa()
        args = m._parse_args(
            ["--mode", "search", "--tier", "noida", "--tier", "delhi"],
        )
        self.assertEqual(args.tiers, ["noida", "delhi"])

    def test_numeric_flags(self):
        m = _exa()
        args = m._parse_args(
            [
                "--mode",
                "all",
                "--agent-batches",
                "7",
                "--similar-per-seed",
                "12",
            ],
        )
        self.assertEqual(args.agent_batches, 7)
        self.assertEqual(args.similar_per_seed, 12)

    def test_no_flags_uses_parser_defaults(self):
        m = _exa()
        args = m._parse_args([])
        self.assertEqual(args.modes, [])
        self.assertIsNone(args.tiers)
        self.assertEqual(args.agent_batches, 4)
        self.assertEqual(args.similar_per_seed, 20)
        self.assertFalse(args.smoke)

    def test_smoke_flag(self):
        m = _exa()
        args = m._parse_args(["--smoke", "--mode", "similar"])
        self.assertTrue(args.smoke)
        self.assertEqual(args.modes, ["similar"])

    def test_invalid_mode_exits(self):
        m = _exa()
        with self.assertRaises(SystemExit):
            m._parse_args(["--mode", "nope"])

    def test_invalid_tier_exits(self):
        m = _exa()
        with self.assertRaises(SystemExit):
            m._parse_args(["--tier", "mumbai"])


class TestCreditErrorDetection(unittest.TestCase):
    """Regression: requestId hex can contain substring '402' — must not flag as out-of-credits."""

    def test_uuid_with_402_substring_not_credit_error(self):
        m = _exa()
        exc = Exception(
            'Request failed with status code 400: {"requestId":"bf22bc0fb02d6ee189fc270d2fc4402c",'
            '"error":"Invalid request body"}',
        )
        self.assertFalse(m._is_credit_error(exc))

    def test_status_402_is_credit_error(self):
        m = _exa()

        class R:
            status_code = 402

        class E(Exception):
            response = R()

        self.assertTrue(m._is_credit_error(E()))

    def test_no_more_credits_message(self):
        m = _exa()
        self.assertTrue(m._is_credit_error(Exception('{"error":"no_more_credits"}')))


class TestRunGuardRails(unittest.TestCase):
    def test_missing_api_key_returns_empty(self):
        m = _exa()
        with patch.object(m, "EXA_API_KEY", ""):
            self.assertEqual(m.run(["similar"]), [])

    def test_placeholder_api_key_returns_empty(self):
        m = _exa()
        with patch.object(m, "EXA_API_KEY", "YOUR_EXA_API_KEY_HERE"):
            self.assertEqual(m.run(["all"]), [])

    def test_sdk_unavailable_returns_empty(self):
        m = _exa()
        with patch.object(m, "EXA_API_KEY", "fake-key-for-test"), patch.object(
            m,
            "EXA_SDK_AVAILABLE",
            False,
        ):
            self.assertEqual(m.run(["agent", "search"]), [])


class TestSubprocessHelp(unittest.TestCase):
    def test_help_zero_exit(self):
        r = subprocess.run(
            [sys.executable, str(EXA_SCRIPT), "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("Exa deep-dive", r.stdout)
        self.assertIn("--mode", r.stdout)
        self.assertIn("--tier", r.stdout)
        self.assertIn("--smoke", r.stdout)


if __name__ == "__main__":
    unittest.main()
