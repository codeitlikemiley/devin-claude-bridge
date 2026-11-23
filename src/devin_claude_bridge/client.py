import httpx

from .config import Account, session_id


class DevinAPIError(ValueError):
    def __init__(self, status_code, message):
        self.status_code = status_code
        super().__init__(message)


class DevinClient:
    def __init__(self, account: Account, transport=None):
        self.account = account
        self.transport = transport

    async def request(self, method, suffix="", **kwargs):
        async with httpx.AsyncClient(
            base_url=self.account.base_url,
            headers={"Authorization": f"Bearer {self.account.api_key}"},
            timeout=30,
            transport=self.transport,
        ) as client:
            try:
                response = await client.request(
                    method, f"/v3/organizations/{self.account.org_id}/sessions{suffix}", **kwargs
                )
            except httpx.RequestError:
                raise ValueError(
                    "Devin API connection failed; check network and base_url"
                ) from None
        if response.is_error:
            try:
                detail = response.json().get("detail") or response.reason_phrase
            except ValueError:
                detail = response.reason_phrase
            labels = {
                401: "Unauthorized (check API key)",
                403: "Forbidden (check permissions)",
                404: "Session or organization not found",
                409: "Session conflict",
                410: "Session unavailable (possibly suspended or terminated)",
            }
            detail = str(detail).replace(self.account.api_key, "[REDACTED]")
            raise DevinAPIError(
                response.status_code,
                f"Devin API {response.status_code}: "
                f"{labels.get(response.status_code, response.reason_phrase)}: {detail}",
            )
        return response.json()

    async def list_sessions(self, limit=100):
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        return await self.request("GET", params={"first": limit})

    async def recent_sessions(self, active_only=True):
        """One page of up to 200 sessions; with active_only, archived ones are left out."""
        params = {"first": 200}
        if active_only:
            params["is_archived"] = "false"
        return (await self.request("GET", params=params))["items"]

    async def sessions_by_id(self, sids):
        """The listed sessions by ID, 100 per request. An unknown ID is simply absent."""
        ids = [session_id(sid) for sid in sids]
        found = {}
        for start in range(0, len(ids), 100):
            params = {"first": 200, "session_ids": ids[start : start + 100]}
            for session in (await self.request("GET", params=params))["items"]:
                found[session_id(session["session_id"])] = session
        return found

    async def root_sessions(self, service_user_id=None):
        params = {"first": 200, "is_archived": "false"}
        if service_user_id:
            params["service_user_ids"] = [service_user_id]
        items = []
        seen = set()
        while True:
            page = await self.request("GET", params=params)
            items.extend(
                s
                for s in page["items"]
                if not s.get("parent_session_id")
                and not s.get("is_archived")
                and (not service_user_id or s.get("service_user_id") == service_user_id)
            )
            if not page.get("has_next_page"):
                return items
            cursor = page.get("end_cursor")
            if not cursor or cursor in seen:
                raise ValueError("Devin API returned a non-advancing pagination cursor")
            seen.add(cursor)
            params["after"] = cursor

    async def create_session(self, prompt, title=None, **options):
        """Create a session. Options that are None are left out of the request."""
        body = {"prompt": prompt, "title": title, **options}
        return await self.request("POST", json={k: v for k, v in body.items() if v is not None})

    async def get_session(self, sid):
        return await self.request("GET", f"/{session_id(sid)}")

    async def send_message(self, sid, message):
        return await self.request("POST", f"/{session_id(sid)}/messages", json={"message": message})

    async def messages(self, sid, after=None):
        items = []
        cursor = after
        seen = set()
        while True:
            params = {"first": 100}
            if cursor:
                params["after"] = cursor
            page = await self.request("GET", f"/{session_id(sid)}/messages", params=params)
            items.extend(page["items"])
            next_cursor = page.get("end_cursor")
            if not page.get("has_next_page"):
                return items, next_cursor or cursor
            if not next_cursor or next_cursor == cursor or next_cursor in seen:
                raise ValueError("Devin API returned a non-advancing pagination cursor")
            seen.add(next_cursor)
            cursor = next_cursor
