"""The SDK's conformance kit, plus what Marvin core relies on for the shared `social.post` capability."""

import pytest
from marvin_integration_sdk import INTEGRATION_REGISTRY, Handle, resolve_policy
from marvin_integration_sdk.testing import IntegrationContract

from marvin_integration_bluesky import BlueskyProvider


class TestBlueskyContract(IntegrationContract):
    @pytest.fixture
    def provider(self):
        return BlueskyProvider()


def test_registers_under_its_slug():
    assert isinstance(INTEGRATION_REGISTRY["bluesky"], BlueskyProvider)


def test_post_declares_the_social_post_contract():
    post = BlueskyProvider().get_action("post")
    assert post.capability == "social.post"
    assert post.requires_approval is True
    props = post.input_schema["properties"]
    assert set(props) == {"text", "images", "alt", "link", "dry_run"}
    assert post.input_schema["required"] == ["text"]
    assert set(post.output_schema["properties"]) >= {"posted", "post_id", "url", "network", "would_post"}


def test_delete_post_is_approval_gated_and_not_a_capability():
    delete = BlueskyProvider().get_action("delete_post")
    assert delete.requires_approval is True
    assert delete.capability is None


def test_secret_and_config_shape():
    p = BlueskyProvider()
    assert [c.key for c in p.credentials] == ["app_password"]
    assert p.config_schema["required"] == ["handle"]
    assert p.config_schema["properties"]["service"]["default"] == "https://bsky.social"


def test_error_policy_covers_auth_and_rate_limits():
    p = BlueskyProvider()
    assert isinstance(resolve_policy(p, "post", "auth"), Handle)
    assert resolve_policy(p, "post", "rate_limited").retry is not None
    assert resolve_policy(p, "post", "unsupported") is None
