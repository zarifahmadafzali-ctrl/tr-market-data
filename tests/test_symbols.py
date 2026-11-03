from downloader.symbols import normalize_symbol, point_divisor


def test_aliases():
    assert normalize_symbol("US30") == "USA30IDXUSD"
    assert normalize_symbol("djia") == "USA30IDXUSD"
    assert normalize_symbol("EURUSD") == "EURUSD"


def test_divisor():
    assert point_divisor("USA30IDXUSD") == 1000.0
    assert point_divisor("EURUSD") == 100_000.0
