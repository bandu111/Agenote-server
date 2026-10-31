"""选择发言尾部的未完成缺口；不另建等待卡片。"""


def gap_targets(sources):
    tails = {}
    for u in sources:
        turn = u.get('turn_id', u.get('turnId')) or u['id']
        speaker = u.get('speaker_id', u.get('speakerId')) or u.get('speaker', '')
        tails[(turn, speaker)] = u
    return [u for u in tails.values()
        if (u.get('turn_status', u.get('turnStatus')) == 'interrupted' or
            any(m in u['raw'] for m in ('……', '...', '…', '[中断]')))
        and not u.get('withdrawn') and not u.get('correction') and not u.get('manual')][-8:]
