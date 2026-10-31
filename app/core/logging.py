import json
import logging
from datetime import datetime, timezone


class RequestFormatter(logging.Formatter):
    def format(self, record):
        # 仅允许固定元数据，绝不序列化 headers、body 或异常上下文。
        result = {"time": datetime.now(timezone.utc).isoformat(), "level": record.levelname}
        for key in ("request_id", "method", "path", "status"):
            if hasattr(record, key):
                result[key] = getattr(record, key)
        return json.dumps(result)


def request_logger():
    logger = logging.getLogger("yanshu.requests")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(RequestFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


class ModelFormatter(logging.Formatter):
    def format(self, record):
        # 仅记录性能与路由，不接收提示词、原话、模型正文或凭据。
        return json.dumps({'kind': 'model_run', **{
            key: getattr(record, key, None) for key in ('role', 'provider', 'model', 'seconds', 'outcome',
                'queueSeconds', 'agentReadySeconds', 'firstTextSeconds', 'completeSeconds')}})


def model_logger():
    logger = logging.getLogger('yanshu.models')
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(ModelFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
