"""Common department and member directory queries."""

from typing import Any


class ContactsAPI:
    def __init__(self, client): self._client = client

    async def get_member(self, userid: str) -> dict[str, Any]:
        return await self._client.get("user/get", {"userid": userid})

    async def create_member(self, member: dict[str, Any]) -> dict[str, Any]:
        return await self._client.post("user/create", member)

    async def update_member(self, member: dict[str, Any]) -> dict[str, Any]:
        return await self._client.post("user/update", member)

    async def delete_member(self, userid: str) -> dict[str, Any]:
        return await self._client.get("user/delete", {"userid": userid})

    async def list_department_members(self, department_id: int, *,
                                      fetch_child: int = 0) -> dict[str, Any]:
        return await self._client.get("user/simplelist", {
            "department_id": department_id, "fetch_child": fetch_child})

    async def list_departments(self, *, department_id: int | None = None
                               ) -> dict[str, Any]:
        body = {} if department_id is None else {"id": department_id}
        return await self._client.get("department/list", body)
