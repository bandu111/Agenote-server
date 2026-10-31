"""从正在生成的 JSON 中提取可展示正文；证据元数据完整之前不展示任何模型文字。"""
import json
from app.modules.meetings.schemas.meeting import Insight, Repair


def _partial_string(text):
    # 网络 token 可截断在转义符或 Unicode 转义内部；只显示已经完整收到的字符。
    if not text.startswith('"'): return None
    for cut in range(min(7, len(text))):
        candidate = text if cut == 0 else text[:-cut]
        try:
            value = json.loads(candidate + '"')
            return value.encode('utf-16-le', 'surrogatepass').decode('utf-16-le', 'ignore')
        except (ValueError, TypeError):
            pass
    return None


def _partial_object(text, content_key='body'):
    decoder = json.JSONDecoder()
    fields, offset = {}, 1
    while offset < len(text):
        offset += len(text[offset:]) - len(text[offset:].lstrip())
        if offset < len(text) and text[offset] == ',': offset += 1
        offset += len(text[offset:]) - len(text[offset:].lstrip())
        try:
            key, end = decoder.raw_decode(text, offset)
        except ValueError: return fields if content_key in fields else None
        if not isinstance(key, str): return None
        offset = end
        offset += len(text[offset:]) - len(text[offset:].lstrip())
        if offset >= len(text) or text[offset] != ':': return None
        offset += 1
        offset += len(text[offset:]) - len(text[offset:].lstrip())
        try:
            value, end = decoder.raw_decode(text, offset)
        except ValueError:
            if key != content_key: return fields if content_key in fields else None
            body = _partial_string(text[offset:])
            if not body: return None
            return {**fields, content_key: body}
        fields[key], offset = value, end
    return fields if content_key in fields else None


def _array_start(text, name):
    """只读取顶层键，不能把正文里提到的 JSON 当作另一条建议。"""
    decoder, offset = json.JSONDecoder(), 0
    while offset < len(text) and text[offset].isspace(): offset += 1
    if offset >= len(text) or text[offset] != '{': return None
    offset += 1
    while offset < len(text):
        while offset < len(text) and text[offset] in ' \n\r\t,': offset += 1
        try: key, offset = decoder.raw_decode(text, offset)
        except ValueError: return None
        while offset < len(text) and text[offset].isspace(): offset += 1
        if offset >= len(text) or text[offset] != ':': return None
        offset += 1
        while offset < len(text) and text[offset].isspace(): offset += 1
        if key == name:
            return offset + 1 if offset < len(text) and text[offset] == '[' else None
        try: _, offset = decoder.raw_decode(text, offset)
        except ValueError: return None
    return None


def _streaming_array(text, name, content_key, limit):
    offset = _array_start(text, name)
    if offset is None: return
    decoder, count = json.JSONDecoder(), 0
    while offset < len(text) and count < limit:
        while offset < len(text) and text[offset] in ' \n\r\t,': offset += 1
        if offset >= len(text) or text[offset] != '{': break
        partial = False
        try: candidate, end = decoder.raw_decode(text, offset)
        except ValueError:
            candidate, end, partial = _partial_object(text[offset:], content_key), len(text), True
        if candidate:
            yield candidate, partial
        count += 1
        if partial: break
        offset = end


def streaming_insights(text, source_ids):
    """只提取 insights 的对象；不把部分 JSON、推理链、工具或业务状态交给前端。"""
    result = []
    for candidate, partial in _streaming_array(text, 'insights', 'body', 2):
        try:
            item = Insight.model_validate(candidate)
            if item.attention != 'defer' and item.kind in {'risk', 'prediction', 'question', 'stitch'} and all(i in source_ids for i in item.evidence_ids):
                result.append({**item.model_dump(), 'streaming': partial})
        except ValueError:
            pass
    return result


def streaming_repairs(text, source_ids):
    """补句的 text 增量也要交付；引用和缺口元数据完整之前不展示。"""
    result = []
    for candidate, partial in _streaming_array(text, 'repairs', 'text', 8):
        try:
            item = Repair.model_validate(candidate)
            if item.target_id in source_ids and all(i in source_ids for i in item.evidence_ids):
                result.append({**item.model_dump(), 'streaming': partial})
        except ValueError:
            pass
    return result
