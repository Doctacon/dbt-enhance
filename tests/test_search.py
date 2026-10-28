from dbt_enhance.search import LiteralSearch


def test_literal_search_wraps_in_both_directions_and_is_not_a_regex():
    search = LiteralSearch("a.*b A.*B tail", ".*b")
    assert search.step() == (1, 4)
    assert search.step() == (6, 9)
    assert search.step() == (1, 4)
    assert search.step(previous=True) == (6, 9)
    assert LiteralSearch("plain", ".*").step() is None
    assert LiteralSearch("plain", "").step() is None


def test_search_includes_the_full_source_and_retains_unicode_offsets():
    source = "é first NEEDLE\n" + "filler\n" * 10000 + "Ω needle"
    search = LiteralSearch(source, "needle")
    first = source.index("NEEDLE")
    last = source.index("needle")
    assert search.step() == (first, first + 6)
    assert search.step() == (last, last + 6)
    assert search.step(previous=True) == (first, first + 6)
    assert LiteralSearch(source, "NEEDLE").step(previous=True) == (last, last + 6)


def test_overlapping_literal_matches_are_reachable_in_both_directions():
    search = LiteralSearch("aaa", "aa")
    assert search.step() == (0, 2)
    assert search.step() == (1, 3)
    assert search.step(previous=True) == (0, 2)
    assert search.step(previous=True) == (1, 3)
    assert search.step() == (0, 2)
