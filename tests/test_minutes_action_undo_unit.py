from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone
from fastapi import HTTPException
from app.modules.meetings.services.minutes_actions import action_details, checklist_entry, complete_minutes_action
from app.modules.home.services.weekly_rhythm import weekly_rhythm


async def check_bidirectional_completion():
    now = datetime.now(timezone.utc)
    meeting = SimpleNamespace(id="meeting",client_id="session",deleted=False,revision=1,
        content={"started_at":now.isoformat(),"title":"QA", "elapsed_ms":70000,"ended":True,
                 "utterances":[{"id":"u","raw":"submit report"}]})
    archive = SimpleNamespace(revision=1,details={"actions":[{"text":"submit report","evidence_ids":["u"]}],
        "topics":[],"decisions":[],"open_questions":[]})
    session = SimpleNamespace(scalar=AsyncMock(),commit=AsyncMock())
    with patch("app.modules.meetings.services.minutes_actions.validate_details",side_effect=lambda d,u:d):
        def task():
            return checklist_entry(meeting,archive,action_details(meeting,archive)["actions"][0])
        def pending():
            return sum(d["pendingActions"] for d in weekly_rhythm([],{},[],[task()],now=now)["days"])
        first=task()
        assert pending()==1
        session.scalar.side_effect=[meeting,archive]
        done=await complete_minutes_action(session,"owner",first["id"],first["version"])
        assert task()["completed"] and pending()==0
        assert done["version"]==task()["version"]
        session.scalar.side_effect=[meeting,archive]
        try:
            await complete_minutes_action(session,"owner",first["id"],first["version"],completed=False)
        except HTTPException as e:
            assert e.status_code==409
        else:
            raise AssertionError("stale undo accepted")
        session.scalar.side_effect=[meeting,archive]
        undone=await complete_minutes_action(session,"owner",first["id"],done["version"],completed=False)
        assert not task()["completed"] and pending()==1
        assert undone["version"]==task()["version"]
        session.scalar.side_effect=[meeting,archive]
        await complete_minutes_action(session,"owner",first["id"],undone["version"],completed=False)
        assert session.commit.await_count==2
