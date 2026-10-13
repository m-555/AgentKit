"""Token efficiency #5: the dashboard shows what input really costs, and per launch.

"Input including cache" added cache reads at full weight: a pilot project's 64M was mostly
cache reads, which are billed at about a tenth of fresh input. The usage
snapshot now also reports a cost-weighted input (cache reads x 0.10, cache
writes x 1.25, both Anthropic's price ratios, applied to Codex's cached share
too) and the average input per reporting launch, by provider and role, so the
token-efficiency work can be measured.
"""
from __future__ import annotations

from agentkit import project_usage


def test_claude_cache_is_weighted_by_its_price_ratio():
    total = project_usage._empty()
    project_usage._add(total, {"input_tokens": 100, "cached_input_tokens": 10_000, "cache_write_input_tokens": 1_000,
                               "output_tokens": 50, "complete": True}, "claude-code")
    assert total["normalized_input_tokens"] == 11_100
    assert total["cost_weighted_input_tokens"] == 100 + 1_250 + 1_000


def test_codex_cached_share_is_weighted_inside_its_input():
    total = project_usage._empty()
    project_usage._add(total, {"input_tokens": 10_000, "cached_input_tokens": 8_000, "output_tokens": 10,
                               "complete": True}, "codex")
    assert total["normalized_input_tokens"] == 10_000
    assert total["cost_weighted_input_tokens"] == 2_000 + 800


def test_average_input_per_reporting_launch():
    total = project_usage._empty()
    for usage in ({"input_tokens": 1_000, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                   "output_tokens": 1, "complete": True}, {}):
        project_usage._add(total, usage, "claude-code")
    assert total["average_input_per_launch"] == 1_000  # the unreported launch is not averaged in


def test_unreported_usage_weights_nothing():
    total = project_usage._empty()
    project_usage._add(total, {}, "claude-code")
    assert total["cost_weighted_input_tokens"] is None and total["average_input_per_launch"] is None
