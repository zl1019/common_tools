"""预约会议 API: WxWorkAPI/docs/服务端API/会议/预约会议基础管理."""

from typing import Any


class MeetingsAPI:
    def __init__(self, client): self._client = client

    async def create(self, *, admin_userid: str, title: str, meeting_start: int,
                     meeting_duration: int, description: str | None = None,
                     location: str | None = None, agentid: int | None = None,
                     invitees: dict[str, list[str]] | None = None,
                     settings: dict[str, Any] | None = None, cal_id: str | None = None,
                     reminders: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "admin_userid": admin_userid, "title": title,
            "meeting_start": meeting_start, "meeting_duration": meeting_duration,
        }
        optional = {"description": description, "location": location, "agentid": agentid,
                    "invitees": invitees, "settings": settings, "cal_id": cal_id,
                    "reminders": reminders}
        body.update({key: value for key, value in optional.items() if value is not None})
        return await self._client.post("meeting/create", body)

    async def get(self, meeting_id: str) -> dict[str, Any]:
        return await self._client.post("meeting/get_info", {"meetingid": meeting_id})

    async def list_for_user(self, userid: str, *, begin_time: int | None = None,
                            end_time: int | None = None, cursor: str = "0",
                            limit: int = 100) -> dict[str, Any]:
        body: dict[str, Any] = {"userid": userid, "cursor": cursor, "limit": limit}
        if begin_time is not None: body["begin_time"] = begin_time
        if end_time is not None: body["end_time"] = end_time
        return await self._client.post("meeting/get_user_meetingid", body)

    async def update(self, meeting_id: str, *, title: str | None = None,
                     meeting_start: int | None = None, meeting_duration: int | None = None,
                     description: str | None = None, location: str | None = None,
                     remind_time: int | None = None, agentid: int | None = None,
                     invitees: dict[str, list[str]] | None = None,
                     settings: dict[str, Any] | None = None, cal_id: str | None = None,
                     reminders: dict[str, Any] | None = None) -> dict[str, Any]:
        if (meeting_start is None) != (meeting_duration is None):
            raise ValueError("meeting_start and meeting_duration must be supplied together")
        body: dict[str, Any] = {"meetingid": meeting_id}
        optional = {"title": title, "meeting_start": meeting_start,
                    "meeting_duration": meeting_duration, "description": description,
                    "location": location, "remind_time": remind_time, "agentid": agentid,
                    "invitees": invitees, "settings": settings, "cal_id": cal_id,
                    "reminders": reminders}
        body.update({key: value for key, value in optional.items() if value is not None})
        return await self._client.post("meeting/update", body)

    async def cancel(self, meeting_id: str) -> dict[str, Any]:
        return await self._client.post("meeting/cancel", {"meetingid": meeting_id})
