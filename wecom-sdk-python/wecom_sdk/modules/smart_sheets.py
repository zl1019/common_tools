"""Common SmartSheet read/write APIs."""

from typing import Any


class SmartSheetsAPI:
    def __init__(self, client): self._client = client

    async def list_sheets(self, docid: str, *, sheet_id: str | None = None,
                          need_all_type_sheet: bool | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"docid": docid}
        if sheet_id is not None: body["sheet_id"] = sheet_id
        if need_all_type_sheet is not None: body["need_all_type_sheet"] = need_all_type_sheet
        return await self._client.post("wedoc/smartsheet/get_sheet", body)

    async def list_fields(self, docid: str, sheet_id: str, *, view_id: str | None = None,
                          field_ids: list[str] | None = None,
                          field_titles: list[str] | None = None,
                          offset: int = 0, limit: int = 0) -> dict[str, Any]:
        body: dict[str, Any] = {"docid": docid, "sheet_id": sheet_id,
                                "offset": offset, "limit": limit}
        optional = {"view_id": view_id, "field_ids": field_ids, "field_titles": field_titles}
        body.update({key: value for key, value in optional.items() if value is not None})
        return await self._client.post("wedoc/smartsheet/get_fields", body)

    async def list_records(self, docid: str, sheet_id: str, *, view_id: str | None = None,
                           record_ids: list[str] | None = None,
                           key_type: str = "CELL_VALUE_KEY_TYPE_FIELD_TITLE",
                           field_titles: list[str] | None = None,
                           field_ids: list[str] | None = None,
                           sort: list[dict[str, Any]] | None = None,
                           offset: int = 0, limit: int = 0, ver: int | None = None,
                           filter_spec: dict[str, Any] | None = None) -> dict[str, Any]:
        if sort is not None and filter_spec is not None:
            raise ValueError("sort and filter_spec cannot be used together")
        body: dict[str, Any] = {"docid": docid, "sheet_id": sheet_id,
                                "key_type": key_type, "offset": offset, "limit": limit}
        optional = {"view_id": view_id, "record_ids": record_ids,
                    "field_titles": field_titles, "field_ids": field_ids,
                    "sort": sort, "ver": ver, "filter_spec": filter_spec}
        body.update({key: value for key, value in optional.items() if value is not None})
        return await self._client.post("wedoc/smartsheet/get_records", body)

    async def add_sheet(self, docid: str, title: str, *, index: int | None = None
                        ) -> dict[str, Any]:
        properties: dict[str, Any] = {"title": title}
        if index is not None: properties["index"] = index
        return await self._client.post("wedoc/smartsheet/add_sheet", {
            "docid": docid, "properties": properties})

    async def add_field(self, docid: str, sheet_id: str, field: dict[str, Any],
                        ) -> dict[str, Any]:
        return await self._client.post("wedoc/smartsheet/add_fields", {
            "docid": docid, "sheet_id": sheet_id, "fields": [field]})

    async def add_records(self, docid: str, sheet_id: str, records: list[dict[str, Any]],
                          *, key_type: str = "CELL_VALUE_KEY_TYPE_FIELD_TITLE"
                          ) -> dict[str, Any]:
        return await self._client.post("wedoc/smartsheet/add_records", {
            "docid": docid, "sheet_id": sheet_id, "key_type": key_type, "records": records})

    async def update_records(self, docid: str, sheet_id: str, records: list[dict[str, Any]],
                             *, key_type: str = "CELL_VALUE_KEY_TYPE_FIELD_TITLE"
                             ) -> dict[str, Any]:
        return await self._client.post("wedoc/smartsheet/update_records", {
            "docid": docid, "sheet_id": sheet_id, "key_type": key_type, "records": records})
