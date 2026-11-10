# WeCom Async SDK

基于 `WxWorkAPI` 文档封装的异步企业微信 API SDK，首批按 API 模块覆盖预约会议、OA 会议室、智能表格、收集表、通讯录和应用消息。本项目是 `common_tools` monorepo 中可独立安装和发布的 Python 包。

本 SDK 只封装企业微信 API，不实现 OpenDay 业务流程、待办主数据、平台切换逻辑或 OpenDay Agent。业务系统应在 SDK 外定义自己的平台无关接口，并通过企微连接器调用本包；未来接入飞书时，可新增独立飞书 SDK/连接器，不让业务层依赖 `wecom_sdk` 类型。

## 安装

发布到 PyPI 后：

```bash
pip install wecom-sdk-python
```

本地开发安装：

```bash
pip install -e .
```

从 GitHub 安装此仓库中的独立子包，可在应用的 `requirements.txt` 中写：

```text
wecom-sdk-python @ git+https://github.com/zl1019/common_tools.git@v0.1.0#subdirectory=wecom-sdk-python
```

将 `v0.1.0` 替换为实际存在的 tag 或 commit SHA。也可改用 `@main` 跟踪主分支；生产依赖建议固定版本引用。私有仓库需要预先配置 GitHub 认证。

依赖 `httpx`。要求 Python 3.10 或更高版本。

## 使用

```python
import os
from wecom_sdk import WeComClient

async def main():
    async with WeComClient(os.environ["WECOM_CORP_ID"], os.environ["WECOM_APP_SECRET"]) as wecom:
        # Omit location filters to list all rooms visible to the application.
        rooms = await wecom.meeting_rooms.list()
        usage = await wecom.meeting_rooms.get_booking_info(
            start_time=1_900_000_000, end_time=1_900_003_600
        )
        created = await wecom.meetings.create(
            admin_userid="organizer", title="项目讨论",
            meeting_start=1_900_000_000, meeting_duration=3600,
        )
        booking = await wecom.meeting_rooms.book(
            meetingroom_id=1, start_time=1_900_000_000,
            end_time=1_900_003_600, booker="organizer", subject="项目讨论",
        )
        await wecom.messages.send_markdown(
            agentid=1000001, touser="organizer",
            content=f"待办通知：<a href=\"https://openday.example/tasks/123\">查看任务</a>",
        )
```

SDK 自行获取和缓存 `access_token`，遇到 token 过期码时重试一次。也可通过 `access_token=` 注入已有 token，或注入外部 `httpx.AsyncClient`。

## 模块

- `wecom.meetings`：预约会议的创建、详情、成员会议列表、修改和取消。
- `wecom.meeting_rooms`：OA 会议室列表、时段预订查询、直接预约、绑定日程/在线会议、取消预约和预约详情。
- `wecom.smart_sheets`：智能表格子表/字段/记录查询及常用增改。
- `wecom.forms`：收集表创建、详情、编辑、统计和答案读取。
- `wecom.contacts`：成员和部门查询。
- `wecom.messages`：应用消息、文本和 Markdown 通知。
- `wecom.media`：临时素材上传、下载及保存到本地。

业务模块对外方法使用显式参数签名：必填参数无默认值，可选参数有明确类型和默认值，编辑器可直接补全并由类型检查器发现拼写错误。接口中的复杂嵌套结构（如会议设置、收集表定义、智能表格筛选器和记录值）以有类型的 `dict` / `list` 参数传入。响应保留企微原始 JSON 结构；非零 `errcode` 抛出 `WeComAPIError`。

## 实现边界

- 会议室 `get_booking_info` 仅支持单日范围；直接预约只适用于无需审批的会议室，服务端会按 30 分钟取整。
- 会议室列表和预订查询的城市、楼宇、楼层筛选值必须与企微后台登记值匹配；首次查询可省略这些条件。
- `meeting_rooms.list()` 的筛选 schema 固定为 `city: str | None`、`building: str | None`、`floor: str | None`、`equipment: list[int] | None`。传 `building` 必须同时传 `city`；传 `floor` 必须同时传 `city` 和 `building`。首次调用建议 `await wecom.meeting_rooms.list()`，确认返回的登记字段后再筛选。
- 会议室预约/会议/收集表只覆盖文档中对应的企微自建应用权限，需在管理端配置可调用范围。
- 回调事件接收、验签解密、微盘持久文件上传下载、游标自动翻页及所有非首批模块暂不封装；可继续按 `modules/` 扩展。
- SDK 不创建或管理 OpenDay 业务待办；`messages` 只推送通知。

临时素材的 `media_id` 仅有效 3 天，适合消息附件，不等于微盘持久文件的 `fileid`。

API 文档来源：仓库 `WxWorkAPI/docs/服务端API` 中会议、会议室、文档/智能表格、通讯录与消息接收发送文档。
