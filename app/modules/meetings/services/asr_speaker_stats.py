"""仅统计会话内编号，不记录录音、原话、账号、会话标识或供应商凭据。"""
from collections import Counter
from datetime import datetime, timezone
import json
import logging


def anonymous_id(value):
    return value if type(value) is int and 0 <= value <= 9 else None


class AsrSpeakerStats:
    def __init__(self):
        self.provider = Counter()
        self.provider_final = Counter()
        self.forwarded = Counter()
        self.unknown = 0
        self.updates = 0

    def observe_provider(self, payload):
        rows = payload.get('sentences', [])
        if isinstance(rows, dict):
            rows = rows.get('sentence_list', [rows] if 'sentence' in rows else [])
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            speaker = anonymous_id(row.get('speaker_id'))
            if speaker is None:
                self.unknown += 1
            else:
                self.provider[speaker] += 1
                if row.get('sentence_type') == 1:
                    self.provider_final[speaker] += 1

    def observe_forwarded(self, event):
        speaker = anonymous_id(event.get('speaker_id'))
        if speaker is not None:
            self.forwarded[speaker] += 1
        self.updates += event.get('type') == 'speaker_update'

    def snapshot(self):
        return {'kind': 'asr_speaker_summary', 'time': datetime.now(timezone.utc).isoformat(),
            'providerIds': sorted(self.provider), 'providerFinalIds': sorted(self.provider_final),
            'forwardedIds': sorted(self.forwarded), 'unknownProviderEvents': self.unknown,
            'providerEvents': sum(self.provider.values()), 'forwardedEvents': sum(self.forwarded.values()),
            'speakerUpdates': self.updates}

    def log(self):
        logger = logging.getLogger('yanshu.asr.speaker_stats')
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(message)s'))
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.info(json.dumps(self.snapshot()))
