import pytest
from app.modules.progress.services.progress_service import deadline_from_text

@pytest.mark.parametrize('words,expected', [
 ('明天','2030-11-01T23:59:00+08:00'),
 ('后天下午3点','2030-11-02T15:00:00+08:00'),
 ('大后天09:30','2030-11-03T09:30:00+08:00'),
 ('明天25点',None), ('之后再说',None),
])
def test_relative_dates_anchor_to_meeting(words,expected):
 assert deadline_from_text(words,'2030-10-31T10:00:00+08:00') == expected
