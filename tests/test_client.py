import json

import httpx
import pytest

from devin_claude_bridge.client import DevinClient
from devin_claude_bridge.config import Account


async def test_calls_and_pagination():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer secret"
        assert request.url.path.startswith("/v3/organizations/org-one/sessions")
        if request.method == "GET" and request.url.path.endswith("/messages"):
            if request.url.params.get("after") == "page1":
                return httpx.Response(
                    200,
                    json={
                        "items": [{"event_id": "b"}],
                        "end_cursor": "page2",
                        "has_next_page": False,
                    },
                )
            return httpx.Response(
                200,
                json={"items": [{"event_id": "a"}], "end_cursor": "page1", "has_next_page": True},
            )
        return httpx.Response(200, json={"session_id": "devin-abc", "items": []})

    client = DevinClient(Account("org-one", "secret"), httpx.MockTransport(handler))
    await client.list_sessions(12)
    assert str(requests[-1].url.query, "ascii") == "first=12"
    await client.create_session("hello", "Title")
    assert json.loads(requests[-1].content) == {"prompt": "hello", "title": "Title"}
    await client.create_session("hello")
    assert json.loads(requests[-1].content) == {"prompt": "hello"}
    await client.get_session("abc")
    assert requests[-1].url.path.endswith("/devin-abc")
    await client.send_message("devin-abc", "followup")
    assert requests[-1].url.path.endswith("/devin-abc/messages")
    assert json.loads(requests[-1].content) == {"message": "followup"}
    messages, cursor = await client.messages("abc")
    assert [m["event_id"] for m in messages] == ["a", "b"]
    assert cursor == "page2"
    assert dict(requests[-1].url.params) == {"first": "100", "after": "page1"}
    with pytest.raises(ValueError, match="limit"):
        await client.list_sessions(201)
    await client.recent_sessions()
    assert dict(requests[-1].url.params) == {"first": "200", "is_archived": "false"}
    await client.recent_sessions(active_only=False)
    assert dict(requests[-1].url.params) == {"first": "200"}


@pytest.mark.parametrize("status", [401, 403, 404, 409, 410, 429])
async def test_api_errors(status):
    client = DevinClient(
        Account("org-one", "secret"),
        httpx.MockTransport(lambda r: httpx.Response(status, json={"detail": "suspended secret"})),
    )
    with pytest.raises(ValueError, match=f"{status}.*suspended") as exc:
        await client.send_message("abc", "hello")
    assert "secret" not in str(exc.value)


async def test_empty_and_bad_pagination():
    client = DevinClient(
        Account("org-one", "secret"),
        httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"items": [], "end_cursor": None, "has_next_page": False}
            )
        ),
    )
    assert await client.messages("abc", "old") == ([], "old")
    client.transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200, json={"items": [], "end_cursor": "same", "has_next_page": True}
        )
    )
    with pytest.raises(ValueError, match="non-advancing"):
        await client.messages("abc")


async def test_network_and_non_json_error():
    def fail(request):
        raise httpx.ConnectError("failure", request=request)

    client = DevinClient(Account("org-one", "secret"), httpx.MockTransport(fail))
    with pytest.raises(ValueError, match="connection failed"):
        await client.get_session("abc")
    client.transport = httpx.MockTransport(lambda r: httpx.Response(403, text="forbidden"))
    with pytest.raises(ValueError, match="403.*Forbidden"):
        await client.get_session("abc")
