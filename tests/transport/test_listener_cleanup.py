from unittest.mock import patch

import pytest

from feishu_auth_kit.transport import (
    BridgeError,
    EncryptedStore,
    TransportBinding,
    bind_verified_profile,
)
from feishu_auth_kit.transport.realtime_io import listen, listener_alive


@pytest.mark.parametrize("failure", ["credentials", "client", "connect"])
def test_failed_start_restores_lock_and_sdk_state(tmp_path, failure):
    import lark_oapi.ws.client as sdk
    from lark_oapi.core.log import logger

    store = EncryptedStore(tmp_path)
    store.initialize()
    bind_verified_profile(store, TransportBinding("cli_SYNTHETIC", "ou_SYNTHETIC", "oc_SYNTHETIC"))
    original_requests, original_kwargs, disabled = (
        sdk.requests,
        sdk._ws_connect_kwargs,
        logger.disabled,
    )
    before_handler = sdk.loop.get_exception_handler()
    def credentials():
        return {"app_id": "cli_SYNTHETIC", "app_secret": "SYNTHETIC_SECRET"}
    if failure == "credentials":
        with pytest.raises(BridgeError):
            listen(store)
    elif failure == "client":
        with patch.object(sdk.Client, "__init__", side_effect=RuntimeError("SYNTHETIC_FAILURE")):
            with pytest.raises(RuntimeError):
                listen(store, credentials_provider=credentials)
    else:

        async def connect(_):
            raise RuntimeError("SYNTHETIC_FAILURE")

        async def disconnect(_):
            pass

        with (
            patch.object(sdk.Client, "_connect", connect),
            patch.object(sdk.Client, "_disconnect", disconnect),
        ):
            with pytest.raises(RuntimeError):
                listen(store, credentials_provider=credentials)
    assert listener_alive(store.private / "realtime") is False
    assert sdk.requests is original_requests and sdk._ws_connect_kwargs is original_kwargs
    assert logger.disabled is disabled and sdk.loop.get_exception_handler() is before_handler
