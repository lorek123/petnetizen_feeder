"""
Per-model feature support, from the Pet Netizen app's ble-device-type.json (app 2.3.6).

A feature counts as supported only when the app enables it for the model (its
``params`` flag) and the model's DP mapping uses the command code this library
sends for it.  The app never exposes long ring, so no model lists it.
"""

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class ModelFeatures:
    """Optional features of one feeder model."""

    model: str
    child_lock: bool
    prompt_sound: bool
    indicator_light: bool  # LED
    auto_lock: bool
    do_not_disturb: bool
    atmosphere_light: bool
    power_source: bool  # mains/battery report (CMD 0E) and the low-battery fault bit
    battery_percentage: bool


# Keyed by the advertised BLE name prefix (the app's bleName).
MODEL_FEATURES: Dict[str, ModelFeatures] = {
    "DU-CP01B": ModelFeatures("DU-CP01B", child_lock=False, prompt_sound=False, indicator_light=False, auto_lock=False, do_not_disturb=False, atmosphere_light=False, power_source=False, battery_percentage=True),
    "Du-F03B": ModelFeatures("Du-F03B", child_lock=True, prompt_sound=True, indicator_light=True, auto_lock=False, do_not_disturb=False, atmosphere_light=True, power_source=True, battery_percentage=False),
    "Du-F06": ModelFeatures("Du-F06B", child_lock=True, prompt_sound=False, indicator_light=True, auto_lock=False, do_not_disturb=False, atmosphere_light=False, power_source=True, battery_percentage=False),
    "Du-F08B": ModelFeatures("Du-F08B", child_lock=True, prompt_sound=False, indicator_light=True, auto_lock=False, do_not_disturb=False, atmosphere_light=False, power_source=True, battery_percentage=False),
    "Du-F09B": ModelFeatures("Du-F09B", child_lock=False, prompt_sound=False, indicator_light=False, auto_lock=False, do_not_disturb=True, atmosphere_light=False, power_source=True, battery_percentage=False),
    "DU-F14B": ModelFeatures("DU-F14B", child_lock=False, prompt_sound=False, indicator_light=True, auto_lock=False, do_not_disturb=False, atmosphere_light=False, power_source=False, battery_percentage=True),
    "Du-F16B": ModelFeatures("Du-F16B", child_lock=False, prompt_sound=False, indicator_light=False, auto_lock=False, do_not_disturb=True, atmosphere_light=False, power_source=True, battery_percentage=False),
    "DU-PD01": ModelFeatures("DU-PD01B", child_lock=True, prompt_sound=False, indicator_light=False, auto_lock=True, do_not_disturb=False, atmosphere_light=False, power_source=True, battery_percentage=False),
    "Du-TC02": ModelFeatures("Du-TC02", child_lock=True, prompt_sound=True, indicator_light=True, auto_lock=True, do_not_disturb=False, atmosphere_light=False, power_source=False, battery_percentage=False),
    "Du-W10B": ModelFeatures("Du-W10B", child_lock=True, prompt_sound=True, indicator_light=True, auto_lock=False, do_not_disturb=False, atmosphere_light=False, power_source=True, battery_percentage=False),
    "Du-W11B": ModelFeatures("Du-W11B", child_lock=True, prompt_sound=True, indicator_light=True, auto_lock=False, do_not_disturb=False, atmosphere_light=False, power_source=True, battery_percentage=False),
    "Du-W12B": ModelFeatures("Du-W12B", child_lock=True, prompt_sound=True, indicator_light=True, auto_lock=False, do_not_disturb=False, atmosphere_light=False, power_source=True, battery_percentage=False),
    "Du-W15B": ModelFeatures("Du-W15B", child_lock=True, prompt_sound=True, indicator_light=True, auto_lock=False, do_not_disturb=False, atmosphere_light=False, power_source=True, battery_percentage=False),
}


def model_features(ble_name: Optional[str]) -> Optional[ModelFeatures]:
    """Return the features for an advertised name like ``Du-W11B_E6C00709A3D3``.

    Returns None for unknown models, so callers can fall back to exposing
    everything.
    """
    if not ble_name:
        return None
    name = ble_name.upper()
    for prefix in sorted(MODEL_FEATURES, key=len, reverse=True):
        if name.startswith(prefix.upper()):
            return MODEL_FEATURES[prefix]
    return None
