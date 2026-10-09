import struct

import pytest
from azure.core.credentials import AccessToken
from azure.identity import (
    AzureCliCredential,
    ClientSecretCredential,
    DefaultAzureCredential,
    ManagedIdentityCredential,
)
from prefect_sqlalchemy.credentials import (
    AsyncDriver,
    ConnectionComponents,
    SyncDriver,
)
from prefect_sqlalchemy.database import (
    AsyncSqlAlchemyConnector,
    SqlAlchemyConnector,
)
from prefect_sqlalchemy.entra import (
    DEFAULT_ENTRA_SCOPE,
    SQL_COPT_SS_ACCESS_TOKEN,
    EntraCredentialType,
    EntraIdAuth,
    encode_access_token,
    register_entra_token_provider,
)
from sqlalchemy import create_engine


class FakeCredential:
    def __init__(self, token="fake-token"):
        self.token = token
        self.scopes = []

    def get_token(self, *scopes, **kwargs):
        self.scopes.append(scopes)
        return AccessToken(self.token, 9999999999)


def decode_access_token(token_struct: bytes) -> str:
    (length,) = struct.unpack_from("<I", token_struct)
    assert len(token_struct) == 4 + length
    return token_struct[4:].decode("UTF-16-LE")


def fire_do_connect(engine, cargs=None, cparams=None):
    cargs = cargs if cargs is not None else []
    cparams = cparams if cparams is not None else {}
    engine.dialect.dispatch.do_connect(engine.dialect, None, cargs, cparams)
    return cargs, cparams


def test_encode_access_token():
    token_struct = encode_access_token("abc")
    assert token_struct == struct.pack("<I6s", 6, "abc".encode("UTF-16-LE"))
    assert decode_access_token(token_struct) == "abc"


class TestRegisterEntraTokenProvider:
    def test_injects_token(self):
        engine = create_engine("sqlite://")
        credential = FakeCredential("tok-1")
        register_entra_token_provider(engine, credential)

        _, cparams = fire_do_connect(engine)

        token_struct = cparams["attrs_before"][SQL_COPT_SS_ACCESS_TOKEN]
        assert decode_access_token(token_struct) == "tok-1"
        assert credential.scopes == [(DEFAULT_ENTRA_SCOPE,)]

    def test_fetches_token_per_connection(self):
        engine = create_engine("sqlite://")
        credential = FakeCredential()
        register_entra_token_provider(engine, credential, scope="custom/.default")

        fire_do_connect(engine)
        fire_do_connect(engine)

        assert credential.scopes == [("custom/.default",)] * 2

    def test_preserves_existing_attrs_before(self):
        engine = create_engine("sqlite://")
        register_entra_token_provider(engine, FakeCredential())

        _, cparams = fire_do_connect(engine, cparams={"attrs_before": {113: 5}})

        assert cparams["attrs_before"][113] == 5
        assert SQL_COPT_SS_ACCESS_TOKEN in cparams["attrs_before"]

    def test_strips_trusted_connection_from_positional_dsn(self):
        engine = create_engine("sqlite://")
        register_entra_token_provider(engine, FakeCredential())

        cargs, _ = fire_do_connect(
            engine,
            cargs=[
                "DRIVER={ODBC Driver 18 for SQL Server};Server=s;Trusted_Connection=Yes"
            ],
        )

        assert cargs == ["DRIVER={ODBC Driver 18 for SQL Server};Server=s"]

    def test_strips_trusted_connection_from_dsn_keyword(self):
        engine = create_engine("sqlite://")
        register_entra_token_provider(engine, FakeCredential())

        _, cparams = fire_do_connect(
            engine, cparams={"dsn": "Server=s;Trusted_Connection=Yes;Database=d"}
        )

        assert cparams["dsn"] == "Server=s;Database=d"


class TestEntraIdAuth:
    def test_default(self):
        credential = EntraIdAuth().get_credential()
        assert isinstance(credential, DefaultAzureCredential)

    def test_managed_identity(self):
        credential = EntraIdAuth(
            credential_type=EntraCredentialType.MANAGED_IDENTITY, client_id="cid"
        ).get_credential()
        assert isinstance(credential, ManagedIdentityCredential)

    def test_client_secret(self):
        credential = EntraIdAuth(
            credential_type="client_secret",
            tenant_id="00000000-0000-0000-0000-000000000000",
            client_id="cid",
            client_secret="secret",
        ).get_credential()
        assert isinstance(credential, ClientSecretCredential)

    def test_client_secret_missing_fields(self):
        with pytest.raises(ValueError, match="tenant_id, client_secret must be set"):
            EntraIdAuth(
                credential_type="client_secret", client_id="cid"
            ).get_credential()

    def test_azure_cli(self):
        credential = EntraIdAuth(credential_type="azure_cli").get_credential()
        assert isinstance(credential, AzureCliCredential)


class TestSqlAlchemyConnectorEntra:
    @pytest.fixture
    def connection_info(self, tmp_path):
        return ConnectionComponents(
            driver=SyncDriver.SQLITE_PYSQLITE, database=str(tmp_path / "test.db")
        )

    def test_no_entra_by_default(self, connection_info):
        connector = SqlAlchemyConnector(connection_info=connection_info)
        engine = connector.get_engine()
        assert len(engine.dialect.dispatch.do_connect) == 0

    def test_set_token_credential(self, connection_info):
        connector = SqlAlchemyConnector(connection_info=connection_info)
        credential = FakeCredential("runtime-token")
        connector.set_token_credential(credential)

        _, cparams = fire_do_connect(connector.get_engine())

        token_struct = cparams["attrs_before"][SQL_COPT_SS_ACCESS_TOKEN]
        assert decode_access_token(token_struct) == "runtime-token"

    def test_set_token_credential_uses_configured_scope(self, connection_info):
        connector = SqlAlchemyConnector(
            connection_info=connection_info,
            entra_id_auth=EntraIdAuth(scope="custom/.default"),
        )
        credential = FakeCredential()
        connector.set_token_credential(credential)

        fire_do_connect(connector.get_engine())

        assert credential.scopes == [("custom/.default",)]

    def test_entra_id_auth_builds_credential(self, connection_info, monkeypatch):
        credential = FakeCredential("block-token")
        monkeypatch.setattr(EntraIdAuth, "get_credential", lambda self: credential)
        connector = SqlAlchemyConnector(
            connection_info=connection_info, entra_id_auth=EntraIdAuth()
        )

        _, cparams = fire_do_connect(connector.get_engine())

        token_struct = cparams["attrs_before"][SQL_COPT_SS_ACCESS_TOKEN]
        assert decode_access_token(token_struct) == "block-token"

    def test_set_token_credential_after_engine_raises(self, connection_info):
        connector = SqlAlchemyConnector(connection_info=connection_info)
        connector.get_engine()
        with pytest.raises(RuntimeError, match="already been created"):
            connector.set_token_credential(FakeCredential())

    def test_save_and_load(self, connection_info):
        SqlAlchemyConnector(
            connection_info=connection_info,
            entra_id_auth=EntraIdAuth(
                credential_type="client_secret",
                tenant_id="tid",
                client_id="cid",
                client_secret="super-secret",
            ),
        ).save("test-entra-connector", overwrite=True)

        loaded = SqlAlchemyConnector.load("test-entra-connector")

        assert loaded.entra_id_auth.credential_type == EntraCredentialType.CLIENT_SECRET
        assert loaded.entra_id_auth.client_secret.get_secret_value() == "super-secret"


class TestAsyncSqlAlchemyConnectorEntra:
    def test_set_token_credential(self, tmp_path):
        connector = AsyncSqlAlchemyConnector(
            connection_info=ConnectionComponents(
                driver=AsyncDriver.SQLITE_AIOSQLITE,
                database=str(tmp_path / "test.db"),
            )
        )
        connector.set_token_credential(FakeCredential("async-token"))

        _, cparams = fire_do_connect(connector.get_engine().sync_engine)

        token_struct = cparams["attrs_before"][SQL_COPT_SS_ACCESS_TOKEN]
        assert decode_access_token(token_struct) == "async-token"
