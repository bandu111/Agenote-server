"""实时识别路由仅声明协议入口，生命周期由服务层管理。"""
from fastapi import APIRouter, WebSocket, Depends, Request
from app.modules.identity.dependencies.auth import get_current_auth_context
from app.modules.meetings.services.asr_stream_service import run_asr_stream

router = APIRouter(prefix="/meetings/asr", tags=["实时转写"])


@router.get("/capabilities")
async def capabilities(ws_identity=Depends(get_current_auth_context)):
    # 此接口触发普通 HTTP 的令牌刷新；不暴露供应商配置或签名。
    return {"code": 200, "message": "ok", "data": {"protocol": 1, "provider": "tencent", "sample_rate": 16000}}


@router.websocket("/stream")
async def stream(socket: WebSocket):
    await run_asr_stream(socket)


@router.get('/highlight-metrics')
async def highlight_metrics(request: Request, identity=Depends(get_current_auth_context)):
    metrics = request.app.state.highlight_metrics.get(identity[0].id)
    return {'code': 200, 'message': 'ok', 'data': metrics.summary() if metrics else {'counts': {}, 'stages_ms': {}}}
