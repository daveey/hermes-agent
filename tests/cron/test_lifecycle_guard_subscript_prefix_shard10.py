"""Wrapper shard 10 of 14 of the prior-expansion cross product (per-file time cap)."""

import pytest

from tests.cron.test_lifecycle_guard_subscript_prefix import (
    BUILTINS, PRIOR, check_prior_expansion, write_marker_payload,
)
from tests.cron.test_lifecycle_guard_subscript_quotes import NAMES, WRAPPERS


@pytest.fixture(scope="module")
def marker_payload(tmp_path_factory):
    return write_marker_payload(tmp_path_factory)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("prior", PRIOR)
@pytest.mark.parametrize("builtin", BUILTINS)
@pytest.mark.parametrize("wrapper", WRAPPERS[10::14])
def test_prior_expansion_cross_product_shard(tmp_path, marker_payload, name, prior, builtin, wrapper):
    check_prior_expansion(tmp_path, marker_payload, name, prior, builtin, wrapper)
