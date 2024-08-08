# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
import asyncio
import os
import urllib.parse
from asyncio import CancelledError
from asyncio import create_task
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Iterator
from typing import NoReturn
from unittest.mock import patch

import httpx
import pytest
import sqlalchemy
from httpx import AsyncClient
from httpx import BasicAuth
from pytest import Config
from pytest import Item
from pytest import MonkeyPatch
from respx import MockRouter
from sqlalchemy import Connection
from sqlalchemy import text


def pytest_configure(config: Config) -> None:
    config.addinivalue_line(
        "markers", "integration_test: mark test as an integration test."
    )


def pytest_collection_modifyitems(items: list[Item]) -> None:
    """Automatically use convenient fixtures for tests marked with integration_test."""

    for item in items:
        if item.get_closest_marker("integration_test"):
            # MUST prepend to replicate auto-use fixtures coming first
            item.fixturenames[:0] = [  # type: ignore[attr-defined]
                "fastramqpi_database_setup",
                "fastramqpi_database_isolation",
                "amqp_event_emitter",
                "os2mo_database_snapshot_and_restore",
                "amqp_queue_isolation",
                "passthrough_backing_services",
            ]
        else:  # unit-test
            # MUST prepend to replicate auto-use fixtures coming first
            item.fixturenames[:0] = [  # type: ignore[attr-defined]
                "empty_environment",
            ]


@pytest.fixture
async def empty_environment() -> AsyncIterator[None]:
    """Clear all environmental variables before running unit-test."""
    with patch.dict(os.environ, clear=True):
        yield


@pytest.fixture(scope="session")
def _settings() -> Any:
    """Access FastRAMQPI settings without coupling to the integration's settings."""
    # We must defer importing from the FastRAMQPI module till run-time.
    # https://github.com/pytest-dev/pytest-cov/issues/587
    from fastramqpi.config import Settings

    class _Settings(Settings):
        class Config:
            env_prefix = "FASTRAMQPI__"

    return _Settings()


@pytest.fixture
async def mo_client(_settings: Any) -> AsyncIterator[AsyncClient]:
    """HTTPX client with the OS2mo URL preconfigured."""
    async with httpx.AsyncClient(base_url=_settings.mo_url) as client:
        yield client


@pytest.fixture
async def rabbitmq_management_client(_settings: Any) -> AsyncIterator[AsyncClient]:
    """HTTPX client for the RabbitMQ management API."""
    amqp = _settings.amqp.get_url()
    async with httpx.AsyncClient(
        base_url=f"http://{amqp.host}:15672/api/",
        auth=BasicAuth(
            username=amqp.user,
            password=amqp.password,
        ),
    ) as client:
        yield client


@pytest.fixture(scope="session")
def superuser(_settings: Any) -> Iterator[Connection]:
    """Managing databases requires a superuser connection."""
    # Connect to "postgres" since we cannot drop a database while being connected to it.
    # TODO: it would be easier to use our own create_engine() from the database module,
    # but pytest cannot properly share event-loops across session-scoped fixtures and
    # (function-scoped) tests. Therefore, we use sqlalchemy's *sync* engine instead.
    # https://github.com/pytest-dev/pytest-asyncio/issues/706#issuecomment-1838860535
    db = _settings.database
    url = f"postgresql+psycopg://{db.user}:{db.password}@{db.host}:{db.port}/postgres"
    engine = sqlalchemy.create_engine(url)
    # AUTOCOMMIT disables transactions to allow for create/drop database operations
    engine.update_execution_options(isolation_level="AUTOCOMMIT")
    with engine.begin() as connection:
        yield connection


@pytest.fixture(scope="session")
def fastramqpi_database_setup(superuser: Connection) -> None:
    """Set up testing database template."""
    # Create separate testing template database. We will apply the database migrations
    # to this database once, and then use a copy of it for each test.
    template_db = "test_template"
    superuser.execute(text(f"drop database if exists {template_db}"))
    superuser.execute(text(f"create database {template_db}"))
    # Run migrations
    # TODO: alembic isn't implemented yet so we don't have to do anything here. Tables
    # are created on app startup for now.


@pytest.fixture
def fastramqpi_database_isolation(
    superuser: Connection, monkeypatch: MonkeyPatch
) -> None:
    """Ensure test isolation by resetting the database between tests.

    Automatically used on tests marked as integration_test.
    """
    # Copy template testing database (with migrations applied) to a temporary testing
    # database for the test that's about to run.
    template_db = "test_template"
    test_db = "test"
    superuser.execute(
        text(
            f"""
            select pg_terminate_backend(pid)
            from pg_stat_activity
            where datname = '{test_db}' and pid <> pg_backend_pid()
            """
        )
    )
    superuser.execute(text(f"drop database if exists {test_db}"))
    superuser.execute(text(f"create database {test_db} template {template_db}"))
    # Patch environment so the app under test will connect to this temporary database
    monkeypatch.setenv("FASTRAMQPI__DATABASE__NAME", test_db)


@pytest.fixture
async def amqp_event_emitter(mo_client: AsyncClient) -> AsyncIterator[None]:
    """Continuously, and quickly, emit OS2mo AMQP events during tests.

    Normally, OS2mo emits AMQP events periodically, but very slowly. Even though there
    are no guarantees as to message delivery speed, and we therefore should not design
    our system around such expectation, waiting a long time for tests to pass in the
    pipelines - or to fail during development - is a very poor development experience.

    Automatically used on tests marked as integration_test.
    """

    async def emitter() -> NoReturn:
        while True:
            await asyncio.sleep(3)
            r = await mo_client.post("/testing/amqp/emit")
            r.raise_for_status()

    task = create_task(emitter())
    yield
    task.cancel()
    with suppress(CancelledError):
        # Await the task to ensure potential errors in the fixture itself, such as a
        # wrong URL or misconfigured OS2mo, are returned to the user.
        await task


@pytest.fixture
async def os2mo_database_snapshot_and_restore(
    mo_client: AsyncClient,
) -> AsyncIterator[None]:
    """Ensure test isolation by resetting the OS2mo database between tests.

    Automatically used on tests marked as integration_test.
    """
    r = await mo_client.post("/testing/database/snapshot")
    r.raise_for_status()
    yield
    r = await mo_client.post("/testing/database/restore")
    r.raise_for_status()


@pytest.fixture
async def amqp_queue_isolation(
    rabbitmq_management_client: AsyncClient,
) -> None:
    """Ensure test isolation by deleting all AMQP queues before tests.

    Automatically used on tests marked as integration_test.
    """
    queues = (await rabbitmq_management_client.get("queues")).json()
    # vhost and name must be URL-encoded. This includes `/`, which is normally regarded
    # as safe. This is particularly important for the default AMQP vhost `/`.
    urls = (
        "queues/{vhost}/{name}".format(
            vhost=urllib.parse.quote(q["vhost"], safe=""),
            name=urllib.parse.quote(q["name"], safe=""),
        )
        for q in queues
    )
    deletes = [rabbitmq_management_client.delete(url) for url in urls]
    await asyncio.gather(*deletes)


@pytest.fixture
def passthrough_backing_services(_settings: Any, respx_mock: MockRouter) -> None:
    """Allow calls to the backing services to bypass the RESPX mocking.

    Automatically used on tests marked as integration_test.
    """
    # mo and keycloak are named to allow tests to revert the passthrough if needed
    respx_mock.route(name="keycloak", host=_settings.auth_server.host).pass_through()
    respx_mock.route(name="mo", host=_settings.mo_url.host).pass_through()
    # rabbitmq management
    respx_mock.route(host=_settings.amqp.get_url().host).pass_through()
    respx_mock.route(host="localhost").pass_through()


@pytest.fixture
def get_num_queued_messages(
    rabbitmq_management_client: AsyncClient,
) -> Callable[[], Awaitable[int]]:
    """Get number of queued messages in RabbitMQ AMQP."""

    async def _get_num_queued_messages() -> int:
        queues = (await rabbitmq_management_client.get("queues")).json()
        return sum(
            queue.get("messages_ready", 0) + queue.get("messages_unacknowledged", 0)
            for queue in queues
        )

    return _get_num_queued_messages


@pytest.fixture
def get_num_consumed_messages(
    rabbitmq_management_client: AsyncClient,
) -> Callable[[], Awaitable[int]]:
    """Get number of consumed messages in RabbitMQ AMQP."""

    async def _get_num_consumed_messages() -> int:
        queues = (await rabbitmq_management_client.get("queues")).json()
        return sum(queue.get("message_stats", {}).get("ack", 0) for queue in queues)

    return _get_num_consumed_messages
