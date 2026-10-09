"""Microsoft Entra ID (Azure AD) token authentication for SQL Server / Azure SQL"""

import re
import struct
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import event
from sqlalchemy.engine import Engine

if TYPE_CHECKING:
    from logging import Logger

    from azure.core.credentials import TokenCredential

# Connection attribute defined by Microsoft in msodbcsql.h
SQL_COPT_SS_ACCESS_TOKEN = 1256

DEFAULT_ENTRA_SCOPE = "https://database.windows.net/.default"

# SQLAlchemy's pyodbc connector adds this when no username is given; it
# conflicts with an access token, so it must be removed from the connection string.
_TRUSTED_CONNECTION_PATTERN = re.compile(r";?\s*Trusted_Connection\s*=\s*Yes", re.I)


class EntraCredentialType(Enum):
    """
    Supported `azure.identity` credential types.

    Attributes:
        DEFAULT (Enum): [DefaultAzureCredential](https://learn.microsoft.com/python/api/azure-identity/azure.identity.defaultazurecredential)
        MANAGED_IDENTITY (Enum): [ManagedIdentityCredential](https://learn.microsoft.com/python/api/azure-identity/azure.identity.managedidentitycredential)
        CLIENT_SECRET (Enum): [ClientSecretCredential](https://learn.microsoft.com/python/api/azure-identity/azure.identity.clientsecretcredential)
        AZURE_CLI (Enum): [AzureCliCredential](https://learn.microsoft.com/python/api/azure-identity/azure.identity.azureclicredential)
    """  # noqa

    DEFAULT = "default"
    MANAGED_IDENTITY = "managed_identity"
    CLIENT_SECRET = "client_secret"
    AZURE_CLI = "azure_cli"


class EntraIdAuth(BaseModel):
    """
    Settings used to build an `azure.identity` TokenCredential whose access tokens
    are injected into each new ODBC connection.

    Attributes:
        credential_type: The `azure.identity` credential type to use.
        tenant_id: The Entra ID tenant ID; required for `client_secret`,
            optional for `azure_cli`.
        client_id: The application (client) ID; required for `client_secret`,
            optional for `managed_identity` (user-assigned identity) and `default`.
        client_secret: The client secret; required for `client_secret`.
        scope: The scope to request the access token for.
    """

    credential_type: EntraCredentialType = Field(
        default=EntraCredentialType.DEFAULT,
        description="The azure.identity credential type to use.",
    )
    tenant_id: Optional[str] = Field(
        default=None,
        description=(
            "The Entra ID tenant ID; required for client_secret, optional for "
            "azure_cli."
        ),
    )
    client_id: Optional[str] = Field(
        default=None,
        description=(
            "The application (client) ID; required for client_secret, optional for "
            "managed_identity (user-assigned identity) and default."
        ),
    )
    client_secret: Optional[SecretStr] = Field(
        default=None,
        description="The client secret; required for client_secret.",
    )
    scope: str = Field(
        default=DEFAULT_ENTRA_SCOPE,
        description="The scope to request the access token for.",
    )

    def get_credential(self) -> "TokenCredential":
        """
        Build the configured `azure.identity` credential.

        Returns:
            A TokenCredential.
        """
        from azure.identity import (
            AzureCliCredential,
            ClientSecretCredential,
            DefaultAzureCredential,
            ManagedIdentityCredential,
        )

        credential_type = self.credential_type
        if credential_type == EntraCredentialType.DEFAULT:
            kwargs = {}
            if self.client_id:
                kwargs["managed_identity_client_id"] = self.client_id
            return DefaultAzureCredential(**kwargs)
        elif credential_type == EntraCredentialType.MANAGED_IDENTITY:
            return ManagedIdentityCredential(client_id=self.client_id)
        elif credential_type == EntraCredentialType.CLIENT_SECRET:
            missing = [
                name
                for name in ("tenant_id", "client_id", "client_secret")
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError(
                    f"{', '.join(missing)} must be set to use the client_secret "
                    "credential type."
                )
            return ClientSecretCredential(
                tenant_id=self.tenant_id,
                client_id=self.client_id,
                client_secret=self.client_secret.get_secret_value(),
            )
        elif credential_type == EntraCredentialType.AZURE_CLI:
            return AzureCliCredential(tenant_id=self.tenant_id)
        raise ValueError(f"Unsupported credential type: {credential_type!r}")


def encode_access_token(token: str) -> bytes:
    """
    Pack an access token into the structure expected by the Microsoft ODBC driver
    for the `SQL_COPT_SS_ACCESS_TOKEN` connection attribute.

    Args:
        token: The access token.

    Returns:
        The length-prefixed, UTF-16-LE encoded token.
    """
    token_bytes = token.encode("UTF-16-LE")
    return struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)


def register_entra_token_provider(
    engine: Engine,
    credential: "TokenCredential",
    scope: str = DEFAULT_ENTRA_SCOPE,
    logger: Optional["Logger"] = None,
) -> None:
    """
    Register a `do_connect` listener on the engine that injects a fresh Entra ID
    access token into the connection parameters every time the pool opens a new
    DBAPI connection.

    For an `AsyncEngine`, pass `async_engine.sync_engine`.

    Args:
        engine: The synchronous SQLAlchemy engine.
        credential: The `azure.identity` TokenCredential used to acquire tokens.
        scope: The scope to request the access token for.
        logger: Optional logger for debug messages.
    """

    @event.listens_for(engine, "do_connect")
    def provide_token(
        dialect: Any, conn_rec: Any, cargs: List[Any], cparams: Dict[str, Any]
    ) -> None:
        """
        Called before the engine creates a new connection. Injects an Entra ID
        token into the connection parameters.
        """
        if logger is not None:
            logger.debug("Acquiring Entra ID access token for new connection.")

        # sync pyodbc passes the connection string positionally,
        # aioodbc passes it as the `dsn` keyword
        if cargs and isinstance(cargs[0], str):
            cargs[0] = _TRUSTED_CONNECTION_PATTERN.sub("", cargs[0])
        if isinstance(cparams.get("dsn"), str):
            cparams["dsn"] = _TRUSTED_CONNECTION_PATTERN.sub("", cparams["dsn"])

        token = credential.get_token(scope).token
        attrs_before = dict(cparams.get("attrs_before") or {})
        attrs_before[SQL_COPT_SS_ACCESS_TOKEN] = encode_access_token(token)
        cparams["attrs_before"] = attrs_before
