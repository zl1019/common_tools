"""Application message delivery APIs."""

from typing import Any


class MessagesAPI:
    def __init__(self, client): self._client = client

    async def send(self, *, agentid: int, msgtype: str, payload: dict[str, Any],
                   touser: str | None = None, toparty: str | None = None,
                   totag: str | None = None, safe: int | None = None,
                   enable_id_trans: int | None = None,
                   enable_duplicate_check: int | None = None,
                   duplicate_check_interval: int | None = None) -> dict[str, Any]:
        if touser is None and toparty is None and totag is None:
            raise ValueError("one of touser, toparty, or totag is required")
        body: dict[str, Any] = {"agentid": agentid, "msgtype": msgtype, msgtype: payload}
        for key, value in (("touser", touser), ("toparty", toparty), ("totag", totag)):
            if value is not None: body[key] = value
        optional = {"safe": safe, "enable_id_trans": enable_id_trans,
                    "enable_duplicate_check": enable_duplicate_check,
                    "duplicate_check_interval": duplicate_check_interval}
        body.update({key: value for key, value in optional.items() if value is not None})
        return await self._client.post("message/send", body)

    async def send_text(self, *, agentid: int, content: str, touser: str | None = None,
                        toparty: str | None = None, totag: str | None = None,
                        safe: int | None = None, enable_duplicate_check: int | None = None,
                        duplicate_check_interval: int | None = None) -> dict[str, Any]:
        return await self.send(agentid=agentid, msgtype="text", payload={"content": content},
                               touser=touser, toparty=toparty, totag=totag, safe=safe,
                               enable_duplicate_check=enable_duplicate_check,
                               duplicate_check_interval=duplicate_check_interval)

    async def send_markdown(self, *, agentid: int, content: str, touser: str | None = None,
                            toparty: str | None = None, totag: str | None = None,
                            enable_duplicate_check: int | None = None,
                            duplicate_check_interval: int | None = None) -> dict[str, Any]:
        return await self.send(agentid=agentid, msgtype="markdown", payload={"content": content},
                               touser=touser, toparty=toparty, totag=totag,
                               enable_duplicate_check=enable_duplicate_check,
                               duplicate_check_interval=duplicate_check_interval)
