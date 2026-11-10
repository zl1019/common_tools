"""OA meeting-room management and booking APIs."""

from typing import Any


class MeetingRoomsAPI:
    def __init__(self, client): self._client = client

    async def list(self, *, city: str | None = None, building: str | None = None,
                   floor: str | None = None, equipment: list[int] | None = None
                   ) -> dict[str, Any]:
        """List meeting rooms, optionally filtered by registered location/equipment."""
        if building is not None and city is None:
            raise ValueError("city is required when filtering by building")
        if floor is not None and (city is None or building is None):
            raise ValueError("city and building are required when filtering by floor")
        body: dict[str, Any] = {}
        if city is not None: body["city"] = city
        if building is not None: body["building"] = building
        if floor is not None: body["floor"] = floor
        if equipment is not None: body["equipment"] = equipment
        return await self._client.post("oa/meetingroom/list", body)

    async def get_booking_info(self, *, start_time: int | None = None,
                               end_time: int | None = None,
                               meetingroom_id: int | None = None, city: str | None = None,
                               building: str | None = None, floor: str | None = None
                               ) -> dict[str, Any]:
        if building is not None and city is None:
            raise ValueError("city is required when filtering by building")
        if floor is not None and (city is None or building is None):
            raise ValueError("city and building are required when filtering by floor")
        body: dict[str, Any] = {}
        optional = {"start_time": start_time, "end_time": end_time,
                    "meetingroom_id": meetingroom_id, "city": city,
                    "building": building, "floor": floor}
        body.update({key: value for key, value in optional.items() if value is not None})
        return await self._client.post("oa/meetingroom/get_booking_info", body)

    async def book(self, *, meetingroom_id: int, start_time: int, end_time: int,
                   booker: str, subject: str | None = None,
                   attendees: list[str] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"meetingroom_id": meetingroom_id, "start_time": start_time,
                                "end_time": end_time, "booker": booker}
        if subject is not None: body["subject"] = subject
        if attendees is not None: body["attendees"] = attendees
        return await self._client.post("oa/meetingroom/book", body)

    async def book_by_schedule(self, *, meetingroom_id: int, schedule_id: str,
                               booker: str) -> dict[str, Any]:
        return await self._client.post("oa/meetingroom/book_by_schedule", {
            "meetingroom_id": meetingroom_id, "schedule_id": schedule_id, "booker": booker})

    async def book_by_meeting(self, *, meetingroom_id: int, meeting_id: str,
                              booker: str) -> dict[str, Any]:
        return await self._client.post("oa/meetingroom/book_by_meeting", {
            "meetingroom_id": meetingroom_id, "meetingid": meeting_id, "booker": booker})

    async def cancel_booking(self, booking_id: str, *, keep_schedule: int | None = None,
                             cancel_date: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"booking_id": booking_id}
        if keep_schedule is not None: body["keep_schedule"] = keep_schedule
        if cancel_date is not None: body["cancel_date"] = cancel_date
        return await self._client.post("oa/meetingroom/cancel_book", body)

    async def get_booking(self, *, meetingroom_id: int, booking_id: str) -> dict[str, Any]:
        return await self._client.post("oa/meetingroom/bookinfo/get", {
            "meetingroom_id": meetingroom_id, "booking_id": booking_id})
