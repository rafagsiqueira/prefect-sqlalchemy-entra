# prefect-sqlalchemy-entra

A fork of [prefect-sqlalchemy](https://github.com/PrefectHQ/prefect/tree/main/src/integrations/prefect-sqlalchemy)
that adds **Microsoft Entra ID (Azure AD) token authentication** for SQL Server and
Azure SQL, using any `TokenCredential` from [`azure.identity`](https://learn.microsoft.com/python/api/overview/azure/identity-readme).

It is a drop-in replacement: it installs the same `prefect_sqlalchemy` module and the
same `SqlAlchemyConnector` / `AsyncSqlAlchemyConnector` blocks, so it must not be
installed alongside `prefect-sqlalchemy`. See the
[upstream docs](https://docs.prefect.io/integrations/prefect-sqlalchemy) for
everything else.

## How it works

When Entra ID authentication is configured, the connector registers a SQLAlchemy
`do_connect` listener on its engine. Each time the pool opens a new DBAPI connection,
the listener requests an access token from the credential and passes it to the
Microsoft ODBC driver through the `SQL_COPT_SS_ACCESS_TOKEN` (1256) connection
attribute. It also strips the `Trusted_Connection=Yes` that SQLAlchemy adds when no
username is set, because that option conflicts with an access token.

Requirements: the `mssql+pyodbc` (sync) or `mssql+aioodbc` (async) driver, and the
[Microsoft ODBC Driver for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server).
Do not set a username or password in the connection info.

## Usage

### Pass a credential at runtime

```python
from azure.identity import DefaultAzureCredential
from prefect_sqlalchemy import ConnectionComponents, SqlAlchemyConnector, SyncDriver

connector = SqlAlchemyConnector(
    connection_info=ConnectionComponents(
        driver=SyncDriver.MSSQL_PYODBC,
        host="myserver.database.windows.net",
        port=1433,
        database="mydb",
        query={"driver": "ODBC Driver 18 for SQL Server"},
    )
)
connector.set_token_credential(DefaultAzureCredential())

with connector:
    print(connector.fetch_one("SELECT SUSER_SNAME()"))
```

`set_token_credential` accepts any synchronous `TokenCredential` and must be called
before the engine is created. The credential is not saved with the block.

### Configure it on a saved block

The `entra_id_auth` field is saved with the block, so it can also be set from the
Prefect UI:

```python
from prefect_sqlalchemy import (
    ConnectionComponents, EntraIdAuth, SqlAlchemyConnector, SyncDriver
)

SqlAlchemyConnector(
    connection_info=ConnectionComponents(
        driver=SyncDriver.MSSQL_PYODBC,
        host="myserver.database.windows.net",
        port=1433,
        database="mydb",
        query={"driver": "ODBC Driver 18 for SQL Server"},
    ),
    entra_id_auth=EntraIdAuth(credential_type="managed_identity", client_id="..."),
).save("azure-sql")

with SqlAlchemyConnector.load("azure-sql") as connector:
    ...
```

| `credential_type`  | `azure.identity` class      | Fields used                                  |
| ------------------ | --------------------------- | -------------------------------------------- |
| `default`          | `DefaultAzureCredential`    | `client_id` (user-assigned managed identity) |
| `managed_identity` | `ManagedIdentityCredential` | `client_id` (optional)                       |
| `client_secret`    | `ClientSecretCredential`    | `tenant_id`, `client_id`, `client_secret`    |
| `azure_cli`        | `AzureCliCredential`        | `tenant_id` (optional)                       |

`scope` defaults to `https://database.windows.net/.default`. A credential passed to
`set_token_credential` takes precedence over the one built from `entra_id_auth`, but
the `scope` from `entra_id_auth` still applies.

### Async

`AsyncSqlAlchemyConnector` works the same way with `AsyncDriver.MSSQL_AIOODBC`
(install `aioodbc`). Use a synchronous credential: tokens are requested inside
SQLAlchemy's sync connect hook.

## Development

```bash
uv sync
uv run pytest
```
