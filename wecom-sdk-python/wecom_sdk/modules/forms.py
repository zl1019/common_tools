"""WeCom collection-form APIs."""

from typing import Any


class FormsAPI:
    def __init__(self, client): self._client = client

    async def create(self, form_info: dict[str, Any], *, spaceid: str | None = None,
                     fatherid: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"form_info": form_info}
        if spaceid is not None: body["spaceid"] = spaceid
        if fatherid is not None: body["fatherid"] = fatherid
        return await self._client.post("wedoc/create_form", body)

    async def get(self, form_id: str) -> dict[str, Any]:
        return await self._client.post("wedoc/get_form_info", {"formid": form_id})

    async def update(self, form_id: str, *, operation: int,
                     form_info: dict[str, Any]) -> dict[str, Any]:
        if operation not in (1, 2):
            raise ValueError("operation must be 1 (questions) or 2 (settings)")
        return await self._client.post("wedoc/modify_form", {
            "oper": operation, "formid": form_id, "form_info": form_info})

    async def statistics(self, repeated_id: str, *, request_type: int,
                         start_time: int | None = None, end_time: int | None = None,
                         limit: int | None = None, cursor: int | None = None
                         ) -> dict[str, Any]:
        if request_type not in (1, 2, 3):
            raise ValueError("request_type must be 1 (summary), 2 (submitted), or 3 (unsubmitted)")
        body: dict[str, Any] = {"repeated_id": repeated_id, "req_type": request_type}
        optional = {"start_time": start_time, "end_time": end_time,
                    "limit": limit, "cursor": cursor}
        body.update({key: value for key, value in optional.items() if value is not None})
        if request_type == 2 and (start_time is None or end_time is None):
            raise ValueError("start_time and end_time are required for submitted-list queries")
        return await self._client.post("wedoc/get_form_statistic", body)

    async def answers(self, repeated_id: str, answer_ids: list[int]) -> dict[str, Any]:
        if not answer_ids or len(answer_ids) > 100:
            raise ValueError("answer_ids must contain between 1 and 100 IDs")
        return await self._client.post("wedoc/get_form_answer", {
            "repeated_id": repeated_id, "answer_ids": answer_ids})
