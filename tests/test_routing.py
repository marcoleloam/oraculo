from oraculo.config import Config, ProviderConfig
from oraculo.routing import Router


def setup_router():
    config = Config(
        providers={
            "codex": ProviderConfig(
                instance_id="codex", billing_verified=True, models=["c"]
            ),
            "grok": ProviderConfig(
                instance_id="grok", billing_verified=False, models=["g"]
            ),
            "claude": ProviderConfig(
                instance_id="claude", billing_verified=True, models=["a"]
            ),
        }
    )
    return Router(config)


def test_unavailable_provider_no_silent_fallback():
    router = setup_router()
    catalog = {
        "providers": [
            {"providerInstanceId": "codex", "available": False, "models": ["c"]},
            {"providerInstanceId": "grok", "models": ["g"]},
        ]
    }
    assert router.select("implementation", catalog) is None
    assert router.last_reason == "unavailable"


def test_quota_never_enables_paid_destination():
    router = setup_router()
    catalog = {
        "providers": [
            {"providerInstanceId": "codex", "models": ["c"]},
            {"providerInstanceId": "grok", "models": ["g"]},
            {
                "providerInstanceId": "claude",
                "models": [{"id": "a", "requiresAdditionalSpend": True}],
            },
        ]
    }
    assert router.select("implementation", catalog, now=100).provider == "codex"
    router.mark_quota("codex", now=100)
    assert router.select("implementation", catalog, now=101) is None
    assert router.select("implementation", catalog, now=2000).provider == "codex"


def test_explicit_target_ignores_allowlist_but_requires_host_catalog():
    router = setup_router()
    catalog = {
        "providers": [
            {"providerInstanceId": "claude", "models": ["a", "opus"]},
            {"providerInstanceId": "grok", "models": ["g"]},
        ]
    }
    # Outside the allowlist and unverified billing: still honored.
    assert (
        router.candidates(
            "implementation",
            catalog,
            explicit={"providerInstanceId": "claude", "model": "opus"},
        )[0].model
        == "opus"
    )
    assert (
        router.candidates(
            "implementation",
            catalog,
            explicit={"providerInstanceId": "grok", "model": "g"},
        )[0].provider
        == "grok"
    )
    assert (
        router.candidates("implementation", catalog, explicit={"model": "opus"})[
            0
        ].provider
        == "claude"
    )
    assert not router.candidates(
        "implementation",
        catalog,
        explicit={"providerInstanceId": "claude", "model": "x"},
    )
    # Automatic ranking still restricted to the allowlist.
    assert [t.model for t in router.candidates("implementation", catalog)] == ["a"]
