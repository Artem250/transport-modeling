from __future__ import annotations


LOS_THRESHOLDS = {
    "A": 0.20,
    "B": 0.45,
    "C": 0.70,
    "D": 0.90,
    "E": 1.00,
    "F": float("inf"),
}
TARGET_LOS_VC = 0.70


def determine_los(vc_ratio: float) -> str:
    for grade, threshold in LOS_THRESHOLDS.items():
        if vc_ratio <= threshold:
            return grade
    return "F"
