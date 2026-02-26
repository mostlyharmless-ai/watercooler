from custom_bench_app.normalize import normalize_identifier


def test_normalize_preserves_underscores():
    # Decision constraint: underscores are semantic separators and must be preserved.
    assert normalize_identifier("Foo_Bar") == "foo_bar"


def test_normalize_strips_other_punctuation():
    assert normalize_identifier("Foo-Bar") == "foobar"
    assert normalize_identifier("Foo.Bar") == "foobar"

