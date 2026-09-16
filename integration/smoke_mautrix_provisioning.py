"""Smoke the exact pinned mautrix provisioning API from the integration container.

This deliberately stops before submitting real Meta credentials. Starting and
cancelling login flows exercises the real BridgeV2 API, generated provisioning
secret, Matrix admin permissions and exact response contract without making a
real account login attempt against Meta.
"""
from __future__ import annotations

from meta_provisioning import (
    MautrixProvisioningClient,
    ProvisioningConfig,
    ProvisioningError,
    default_config,
    safe_step,
)


def main() -> None:
    config = default_config()
    client = MautrixProvisioningClient(config)

    assert config.shared_secret not in {"", "generate", "disable"}
    assert len(config.shared_secret) >= 16

    whoami = client.whoami()
    assert isinstance(whoami, dict), "whoami did not return an object"
    network = whoami.get("network") or {}
    assert isinstance(network, dict), "whoami.network is not an object"

    flows = client.flows()
    flow_ids = {str(flow.get("id") or "") for flow in flows}
    print("available Meta login flows:", sorted(flow_ids), flush=True)
    assert "facebook" in flow_ids, f"facebook flow missing: {sorted(flow_ids)}"
    assert "messenger" in flow_ids, f"messenger flow missing: {sorted(flow_ids)}"

    step = client.start("facebook")
    sanitized = safe_step(step)
    assert sanitized.get("type") == "cookies", f"unexpected first step: {sanitized!r}"
    assert sanitized.get("login_id"), "login process id missing"
    assert sanitized.get("step_id"), "step id missing"

    cookies = sanitized.get("cookies") or {}
    url = str(cookies.get("url") or "")
    assert url.startswith("https://") and "facebook.com" in url, f"unexpected Meta login URL: {url!r}"
    fields = cookies.get("fields") or []
    required_ids = {str(field.get("id") or "") for field in fields if field.get("required", True)}
    assert "c_user" in required_ids, f"c_user missing from required fields: {sorted(required_ids)}"
    assert "xs" in required_ids, f"xs missing from required fields: {sorted(required_ids)}"

    client.cancel(str(sanitized["login_id"]))

    # v26.08 introduced/updated Messenger mobile login modes upstream. If this
    # exact pinned runtime exposes either mode, exercise only the first step and
    # cancel it. Never submit credentials in CI. The printed type gives us direct
    # evidence of whether the deployed build can offer a helper-free UI path.
    for optional_flow in ("messenger-lite", "messenger-lite-android"):
        if optional_flow not in flow_ids:
            print(f"optional login flow not exposed: {optional_flow}", flush=True)
            continue
        optional_step = safe_step(client.start(optional_flow))
        print(f"{optional_flow} first step: {optional_step.get('type')!r}", flush=True)
        assert optional_step.get("login_id"), f"{optional_flow} login process id missing"
        assert optional_step.get("step_id"), f"{optional_flow} step id missing"
        assert optional_step.get("type"), f"{optional_flow} returned no step type"
        client.cancel(str(optional_step["login_id"]))

    post_cancel = client.whoami()
    assert isinstance(post_cancel.get("logins", []), list), "whoami.logins changed shape after cancel"

    bad = MautrixProvisioningClient(ProvisioningConfig(
        base_url=config.base_url,
        user_id=config.user_id,
        shared_secret="ci-invalid-provisioning-secret",
        timeout=config.timeout,
    ))
    try:
        bad.whoami()
    except ProvisioningError as exc:
        assert exc.status_code in {401, 403}, f"unexpected bad-secret status: {exc.status_code}"
    else:
        raise AssertionError("mautrix accepted an invalid provisioning secret")

    print("mautrix provisioning smoke: ok")


if __name__ == "__main__":
    main()
