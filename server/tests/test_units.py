from __future__ import annotations

import pytest
from ulid import ULID

from collab_hub.config import Settings
from collab_hub.errors import HubError
from collab_hub.service import normalize
from collab_hub.store import Cursor, invert_ulid


def test_invert_ulid_reverses_order_and_is_self_inverse():
    ids = sorted(str(ULID()) for _ in range(50))
    inv = [invert_ulid(i) for i in ids]
    assert inv == sorted(inv, reverse=True)
    assert [invert_ulid(i) for i in inv] == ids


def test_cursor_roundtrip_and_rejects_garbage():
    c = Cursor({"NextPartitionKey": "a", "NextRowKey": "b"}, 7)
    d = Cursor.decode(c.encode())
    assert d.token == c.token and d.skip == 7
    for bad in ("%%%", "e30", Cursor(None, -1).encode()):
        with pytest.raises(HubError):
            Cursor.decode(bad if bad != "e30" else "eyJzIjoiYSJ9")


def test_normalize_is_case_and_punctuation_insensitive():
    assert normalize("Azure  Container-Apps, É!") == "azure container apps é"


def test_settings_refuse_empty_principals():
    with pytest.raises(ValueError, match="PRINCIPALS"):
        Settings(
            storage_connection_string="UseDevelopmentStorage=true",
            tenant_id="t",
            oidc_audience="a",
            principals={},
        )


def test_settings_require_storage():
    with pytest.raises(ValueError, match="STORAGE"):
        Settings(tenant_id="t", oidc_audience="a", principals={"x": {"alias": "owner"}})


def test_entra_defaults_derive_from_tenant():
    s = Settings(
        storage_account="acct",
        tenant_id="tid",
        oidc_audience="a",
        principals={"x": {"alias": "owner"}},
        public_url="https://hub.example.com",
    )
    assert s.issuer == "https://login.microsoftonline.com/tid/v2.0"
    assert s.jwks_url == "https://login.microsoftonline.com/tid/discovery/v2.0/keys"
    assert s.resource_url == "https://hub.example.com/mcp"
    assert s.host_allowlist == ["hub.example.com"]
