"""Glob semantics. These decide what an agent may edit, so the edge cases matter.

The central rule under test: `*` must not cross a directory separator. If it did,
`routes/*` would silently own the entire subtree and the ownership model would
grant far more than an author intended.
"""

from agentkit.globs import matches, matches_any, overlaps


class TestExactAndDirectory:
    def test_exact_file(self):
        assert matches("routes/video.py", "routes/video.py")
        assert not matches("routes/video.py", "routes/audio.py")

    def test_bare_directory_owns_contents(self):
        assert matches("routes", "routes/video.py")
        assert matches("routes", "routes/video/images.py")

    def test_trailing_slash_owns_contents(self):
        assert matches("routes/", "routes/video.py")
        assert matches("services/media/", "services/media/providers/veo.py")

    def test_directory_prefix_is_not_substring(self):
        assert not matches("routes", "routes_legacy/video.py")


class TestWildcards:
    def test_single_star_stops_at_separator(self):
        assert matches("routes/*.py", "routes/video.py")
        assert not matches("routes/*.py", "routes/video/images.py")

    def test_double_star_crosses_separators(self):
        assert matches("services/**", "services/media/providers/veo.py")
        assert matches("services/**/*.py", "services/a/b/c.py")

    def test_double_star_matches_zero_directories(self):
        assert matches("services/**/*.py", "services/media.py")

    def test_question_mark(self):
        assert matches("v?.py", "v1.py")
        assert not matches("v?.py", "v10.py")


class TestNormalisation:
    def test_backslashes_are_equivalent(self):
        assert matches("routes/video.py", "routes\\video.py")
        assert matches("routes\\video.py", "routes/video.py")

    def test_leading_dot_slash_ignored(self):
        assert matches("./routes/video.py", "routes/video.py")

    def test_empty_inputs_never_match(self):
        assert not matches("", "routes/video.py")
        assert not matches("routes/video.py", "")


class TestMatchesAny:
    def test_returns_the_matching_pattern(self):
        patterns = ["tests/**", "services/media/**"]
        assert matches_any(patterns, "services/media/veo.py") == "services/media/**"

    def test_returns_none_when_nothing_matches(self):
        assert matches_any(["tests/**"], "routes/video.py") is None

    def test_handles_empty_pattern_list(self):
        assert matches_any([], "routes/video.py") is None


class TestOverlaps:
    """Overlap detection gates whether two tasks may run concurrently.

    It is deliberately conservative: a false positive costs a little concurrency,
    a false negative costs a corrupted merge.
    """

    def test_identical_patterns_overlap(self):
        assert overlaps("services/media.py", "services/media.py")

    def test_subtree_overlaps_file_within_it(self):
        assert overlaps("services/**", "services/media/veo.py")

    def test_sibling_files_do_not_overlap(self):
        assert not overlaps("services/retry.py", "services/media.py")

    def test_sibling_subtrees_do_not_overlap(self):
        assert not overlaps("routes/images/**", "routes/video/**")

    def test_prefix_is_not_confused_with_sibling(self):
        assert not overlaps("routes/video/**", "routes/video_legacy/**")
