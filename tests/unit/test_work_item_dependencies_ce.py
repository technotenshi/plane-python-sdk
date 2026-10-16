"""Dependencies against a Community Edition shaped server.

CE serves ``work-items/<id>/relations/`` (payload key ``issues``, grouped
``{project_id, issue_id}`` references) instead of ``dependencies/``. A fake HTTP
server stands in for Plane, so these tests need no network or credentials.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from plane.client import PlaneClient
from plane.errors.errors import HttpError
from plane.models.work_items import CreateWorkItemDependency

WORKSPACE = "ws"
PROJECT = "11111111-1111-1111-1111-111111111111"
ITEM_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ITEM_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
ITEM_C = "cccccccc-cccc-cccc-cccc-cccccccccccc"
BASE = f"/api/v1/workspaces/{WORKSPACE}/projects/{PROJECT}/work-items"
GROUPS = ("blocking", "blocked_by", "start_before", "start_after", "finish_before", "finish_after")


class FakePlane:
    """Records requests and answers like CE (``relations/``) or modern (``dependencies/``)."""

    def __init__(self, flavour: str) -> None:
        self.flavour = flavour
        self.requests: list[tuple[str, str, Any]] = []
        # (item, related_item, stored type) rows, as CE stores them.
        self.rows: list[tuple[str, str, str]] = []
        # Route -> forced status for a method, e.g. {("dependencies", "POST"): 500}.
        self.forced: dict[tuple[str, str], int] = {}
        self.delay: dict[tuple[str, str], float] = {}

    def relation_posts(self) -> list[Any]:
        return [b for m, path, b in self.requests if m == "POST" and path.endswith("/relations/")]

    def paths(self) -> list[tuple[str, str]]:
        return [(method, path.rsplit("/", 2)[-2]) for method, path, _ in self.requests]

    def handle(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        self.requests.append((method, path, body))
        item, route = path.removeprefix(BASE + "/").strip("/").split("/")
        time.sleep(self.delay.get((route, method), 0))
        if (route, method) in self.forced:
            return self.forced[(route, method)], {"detail": "forced"}
        live = "relations" if self.flavour == "ce" else "dependencies"
        if route != live:
            return 404, {"error": "not found"}
        if self.flavour == "ce":
            return self._ce(method, item, body)
        return self._modern(method, item, body)

    def _ce(self, method: str, item: str, body: Any) -> tuple[int, Any]:
        if method == "GET":
            groups: dict[str, list[dict[str, str]]] = {name: [] for name in GROUPS}
            groups.update(duplicate=[], relates_to=[])
            for first, second, _ in self.rows:
                if second == item:
                    groups["blocking"].append({"project_id": PROJECT, "issue_id": first})
                if first == item:
                    groups["blocked_by"].append({"project_id": PROJECT, "issue_id": second})
            return 200, groups
        assert "work_item_ids" not in body, "CE takes `issues`"
        reverse = body["relation_type"] == "blocking"
        created = []
        for other in body["issues"]:
            row = (other, item, "blocked_by") if reverse else (item, other, "blocked_by")
            if row not in self.rows:
                self.rows.append(row)
            created.append({"id": other, "project_id": PROJECT, "relation_type": "blocked_by"})
        return 201, created

    def _modern(self, method: str, item: str, body: Any) -> tuple[int, Any]:
        if method == "GET":
            return 200, {name: [] for name in GROUPS}
        return 201, [
            {"id": other, "project_id": PROJECT, "relation_type": body["relation_type"]}
            for other in body["work_item_ids"]
        ]


@pytest.fixture
def make_server() -> Iterator[Any]:
    servers: list[ThreadingHTTPServer] = []

    def start(flavour: str) -> tuple[FakePlane, PlaneClient]:
        fake = FakePlane(flavour)

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                status, payload = fake.handle(self.command, self.path.split("?")[0], body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        client = PlaneClient(base_url=f"http://127.0.0.1:{server.server_port}", api_key="k")
        client.config.timeout = 0.5
        return fake, client

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _create(client: PlaneClient, item: str, relation_type: str, targets: list[str]) -> Any:
    return client.work_items.dependencies.create(
        WORKSPACE,
        PROJECT,
        item,
        CreateWorkItemDependency(relation_type=relation_type, work_item_ids=targets),  # type: ignore[arg-type]
    )


def _list(client: PlaneClient, item: str) -> Any:
    return client.work_items.dependencies.list(WORKSPACE, PROJECT, item)


def test_ce_list_reads_relations_and_adapts_the_grouped_references(make_server: Any) -> None:
    fake, client = make_server("ce")
    fake.rows.append((ITEM_A, ITEM_B, "blocked_by"))  # A is blocked by B

    result = _list(client, ITEM_A)

    assert [item.id for item in result.blocked_by] == [ITEM_B]
    assert result.blocked_by[0].project_id == PROJECT
    assert result.blocked_by[0].relation_type == "blocked_by"
    assert result.blocking == []
    assert "relations" in [route for _, route in fake.paths()]


@pytest.mark.parametrize(
    ("relation_type", "blocker", "blocked"),
    [("blocking", ITEM_A, ITEM_B), ("blocked_by", ITEM_B, ITEM_A)],
)
def test_ce_create_sends_issues_and_keeps_the_blocking_direction(
    make_server: Any, relation_type: str, blocker: str, blocked: str
) -> None:
    fake, client = make_server("ce")

    created = _create(client, ITEM_A, relation_type, [ITEM_B])

    post = fake.relation_posts()[0]
    assert post == {"relation_type": relation_type, "issues": [ITEM_B]}
    assert [item.id for item in created] == [ITEM_B]
    # Read back from both ends: the blocker lists the blocked item under `blocking`.
    assert [i.id for i in _list(client, blocker).blocking] == [blocked]
    assert [i.id for i in _list(client, blocked).blocked_by] == [blocker]


def test_ce_create_twice_does_not_duplicate_and_still_answers(make_server: Any) -> None:
    fake, client = make_server("ce")
    _create(client, ITEM_A, "blocking", [ITEM_B])
    posts_before = fake.relation_posts()

    again = _create(client, ITEM_A, "blocking", [ITEM_B])

    assert len(fake.relation_posts()) == len(posts_before)
    assert [item.id for item in again] == [ITEM_B]
    assert fake.rows == [(ITEM_B, ITEM_A, "blocked_by")]


def test_ce_create_only_posts_the_missing_targets(make_server: Any) -> None:
    fake, client = make_server("ce")
    _create(client, ITEM_A, "blocking", [ITEM_B])

    result = _create(client, ITEM_A, "blocking", [ITEM_B, ITEM_C])

    last_post = fake.relation_posts()[-1]
    assert last_post["issues"] == [ITEM_C]
    assert sorted(item.id for item in result) == sorted([ITEM_B, ITEM_C])


def test_modern_server_keeps_using_dependencies(make_server: Any) -> None:
    fake, client = make_server("modern")

    created = _create(client, ITEM_A, "blocking", [ITEM_B])
    _list(client, ITEM_A)

    assert {route for _, route in fake.paths()} == {"dependencies"}
    assert fake.requests[0][2] == {"relation_type": "blocking", "work_item_ids": [ITEM_B]}
    assert created[0].relation_type == "blocking"


@pytest.mark.parametrize("status", [401, 403, 500, 503])
@pytest.mark.parametrize("operation", ["list", "create"])
def test_auth_and_server_errors_are_not_retried_through_relations(
    make_server: Any, status: int, operation: str
) -> None:
    fake, client = make_server("ce")
    method = "GET" if operation == "list" else "POST"
    fake.forced[("dependencies", method)] = status

    with pytest.raises(HttpError) as raised:
        if operation == "list":
            _list(client, ITEM_A)
        else:
            _create(client, ITEM_A, "blocking", [ITEM_B])

    assert raised.value.status_code == status
    assert {route for _, route in fake.paths()} == {"dependencies"}
    assert fake.rows == []


def test_a_write_timeout_is_not_retried_through_relations(make_server: Any) -> None:
    fake, client = make_server("modern")
    fake.delay[("dependencies", "POST")] = 1.5

    with pytest.raises(Exception) as raised:
        _create(client, ITEM_A, "blocking", [ITEM_B])

    assert "imeout" in type(raised.value).__name__ or "imed out" in str(raised.value)
    assert {route for _, route in fake.paths()} == {"dependencies"}


def test_a_404_from_both_routes_raises_the_original_error(make_server: Any) -> None:
    fake, client = make_server("modern")
    fake.forced[("dependencies", "GET")] = 404
    fake.forced[("relations", "GET")] = 404

    with pytest.raises(HttpError) as raised:
        _list(client, ITEM_A)

    assert raised.value.status_code == 404
    assert [route for _, route in fake.paths()] == ["dependencies", "relations"]
