"""Per-model features and fault decoding."""

from petnetizen_feeder import decode_fault_flags, model_features


def test_w11b_features_match_the_app():
    f = model_features("Du-W11B_E6C00709A3D3")
    assert f is not None and f.model == "Du-W11B"
    assert f.child_lock and f.prompt_sound and f.indicator_light and f.power_source
    assert not (f.auto_lock or f.do_not_disturb or f.atmosphere_light)
    assert not f.battery_percentage


def test_name_matching_is_case_insensitive_and_prefers_longest_prefix():
    assert model_features("du-f06b_AABBCCDDEEFF").model == "Du-F06B"  # bleName "Du-F06"
    assert model_features("DU-PD01B_112233445566").model == "DU-PD01B"


def test_f09b_has_do_not_disturb_but_no_child_lock():
    f = model_features("Du-F09B_C8EE10000000")
    assert f.do_not_disturb and not f.child_lock and not f.indicator_light


def test_unknown_model_returns_none():
    assert model_features("JK-FEEDER-1") is None
    assert model_features(None) is None


def test_decode_fault_flags():
    assert decode_fault_flags(0) == {
        "stuck_grain": False, "food_blocked": False, "out_of_food": False, "low_battery": False,
    }
    flags = decode_fault_flags(0x01 | 0x04 | 0x10)
    assert flags["stuck_grain"] and flags["out_of_food"] and flags["low_battery"]
    assert not flags["food_blocked"]
