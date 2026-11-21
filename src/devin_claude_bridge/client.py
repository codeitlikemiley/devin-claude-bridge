import httpx

from .config import Account, session_id


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
            raise ValueError(
                f"Devin API {response.status_code}: "
                f"{labels.get(response.status_code, response.reason_phrase)}: {detail}"
            )
        return response.json()

    async def list_sessions(self, limit=100):
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        return await self.request("GET", params={"first": limit})

    async def create_session(self, prompt, title=None):
        body = {"prompt": prompt}
        if title is not None:
            body["title"] = title
        return await self.request("POST", json=body)

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
