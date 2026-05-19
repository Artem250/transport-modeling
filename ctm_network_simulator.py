"""Stable import name for the CTM network simulator.

The implementation still lives in ``ctm_simulator_test.py`` to avoid breaking
existing scripts and tests. New code should import from this module instead.
"""

from ctm_simulator_test import CTMScenarioConfig, CTMSimulator

__all__ = ["CTMScenarioConfig", "CTMSimulator"]
