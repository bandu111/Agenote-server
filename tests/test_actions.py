"""用真实 PostgreSQL 检查点验证暂停、跨实例恢复和手机回执核验。"""

from unittest.mock import AsyncMock
from tests.test_meetings import account, meeting


async def prepared(client, app, status="confirmed"):
    headers = await account(client)
    source = meeting()
    source["utterances"][0]["raw"] = "确定2030年10月23日14点到15点开进度会。"
    await client.post("/api/v1/meetings/sync", headers=headers, json=source)
    app.state.meeting_analysis.agents.analyze = AsyncMock(
        return_value={
            "insights": [],
            "repairs": [],
            "withdrawals": [],
            "action_intents": [
                {
                    "title": "项目进度会",
                    "start": "2030-10-23T14:00:00+08:00",
                    "end": "2030-10-23T15:00:00+08:00",
                    "status": status,
                    "evidence_ids": ["u1"],
                }
            ],
        }
    )
    result = await client.post(
        "/api/v1/meetings/phone-meeting-1/analysis", headers=headers
    )
    assert result.status_code == 200, result.text
    actions = (await client.get("/api/v1/actions", headers=headers)).json()["data"]
    return headers, actions


async def test_action_checkpoint_resume_device_binding_verification_and_idempotency(
    client, app
):
    headers, actions = await prepared(client, app)
    assert len(actions) == 1
    action = actions[0]
    path = "/api/v1/actions/" + action["id"]
    device = "synthetic-test-device-1"
    decision = {"fingerprint": action["fingerprint"], "allow": True, "deviceId": device}
    approved = await client.post(path + "/decision", headers=headers, json=decision)
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["status"] == "awaiting_device"
    # 每次请求都重新创建图和连接，这里从另一实例读取 PostgreSQL 中断恢复。
    repeated = await client.post(path + "/decision", headers=headers, json=decision)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"]["status"] == "awaiting_device"
    wrong_device = await client.post(
        path + "/decision",
        headers=headers,
        json={**decision, "deviceId": "synthetic-other-device"},
    )
    assert wrong_device.status_code == 409
    receipt = {k: action[k] for k in ["fingerprint", "title", "start", "end"]}
    receipt.update(verified=True, eventId="synthetic-native-event", deviceId=device)
    mismatch = await client.post(
        path + "/receipt", headers=headers, json={**receipt, "title": "错误标题"}
    )
    assert mismatch.status_code == 200, mismatch.text
    assert mismatch.json()["data"]["status"] == "awaiting_device"
    complete = await client.post(path + "/receipt", headers=headers, json=receipt)
    assert complete.status_code == 200, complete.text
    assert complete.json()["data"]["status"] == "completed"
    assert (await client.post(path + "/receipt", headers=headers, json=receipt)).json()[
        "data"
    ]["status"] == "completed"
    other = await account(client, "isolated@example.com")
    assert (await client.get("/api/v1/actions", headers=other)).json()["data"] == []
    assert (
        await client.post(path + "/decision", headers=other, json=decision)
    ).status_code == 404


async def test_proposal_never_creates_action(client, app):
    _, actions = await prepared(client, app, status="proposed")
    assert actions == []


async def test_action_decline_is_durable(client, app):
    headers, actions = await prepared(client, app)
    a = actions[0]
    payload = {
        "fingerprint": a["fingerprint"],
        "allow": False,
        "deviceId": "synthetic-test-device-1",
    }
    result = await client.post(
        f"/api/v1/actions/{a['id']}/decision", headers=headers, json=payload
    )
    assert result.status_code == 200, result.text
    assert result.json()["data"]["status"] == "declined"
    assert (await client.get("/api/v1/actions", headers=headers)).json()["data"][0][
        "status"
    ] == "declined"
