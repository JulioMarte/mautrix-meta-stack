"""Smoke the exact pinned mautrix provisioning API from the integration container.

This deliberately stops before submitting Meta cookies. Starting and cancelling the
facebook flow exercises the real BridgeV2 API, generated provisioning secret,
Matrix admin permissions and exact response contract without needing real Facebook
credentials or making a login attempt against Meta.
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
