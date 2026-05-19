"""Stable import name for the CTM network simulator.

New code should import from this module. The current implementation adds two
small theory-oriented improvements over the historical ctm_simulator_test.py:
lane-aware incidents and finite movement capacities at nodes.
"""

from ctm_theory_simulator import CTMScenarioConfig, CTMSimulator

__all__ = ["CTMScenarioConfig", "CTMSimulator"]
