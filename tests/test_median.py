import statistics

import pytest

from battery_aggregator.core import _median


@pytest.mark.parametrize("data", [[1.0], [3.0, 1.0], [5.0, 1.0, 3.0], [54.1, 54.08, 54.05, 54.09], (x for x in [2, 9, 4])])
def test_median_matches_stdlib(data):
    data = list(data)
    assert _median(data) == statistics.median(data)
    assert _median(iter(data)) == statistics.median(data)


def test_median_empty_raises():
    with pytest.raises(ValueError):
        _median([])


def test_core_does_not_import_statistics():
    import battery_aggregator.core as core
    assert "statistics" not in vars(core)
