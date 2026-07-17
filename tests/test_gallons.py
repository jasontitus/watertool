from watertool.rachio.gallons import estimate_gallons


def test_known_value():
    # 1 in/hr over 1000 sqft for one hour = 1 * 1 * 1000 * 0.6233
    assert estimate_gallons(1.0, 1000, 3600) == 623.3


def test_scales_with_duration():
    full = estimate_gallons(1.0, 1000, 3600)
    half = estimate_gallons(1.0, 1000, 1800)
    assert abs(half - full / 2) < 1e-6


def test_missing_inputs_return_none():
    assert estimate_gallons(None, 1000, 600) is None
    assert estimate_gallons(1.0, None, 600) is None
    assert estimate_gallons(1.0, 1000, None) is None


def test_nonpositive_returns_none():
    assert estimate_gallons(0, 1000, 600) is None
    assert estimate_gallons(1.0, -5, 600) is None
    assert estimate_gallons(1.0, 1000, 0) is None
