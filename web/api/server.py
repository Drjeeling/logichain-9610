"""FastAPI 后端：把 Coordinator 包成 HTTP 接口。

启动：
    cd /workspace/transport_agent
    uvicorn web.api.server:app --host 0.0.0.0 --port 8000 --reload

接口：
    POST /api/dispatch   { query, month? }       → 4个Agent协同结果
    POST /api/routes     { origin, dest, month }  → 仅 transport 方案
    GET  /api/nodes                              → 节点列表（含经纬度，给前端画图）
    POST /api/ocr       { image_path }           → OCR 识图
"""
from __future__ import annotations

import base64
import io
import os
import sys
import json
from pathlib import Path
from typing import Any, Dict, Optional

# 让 Python 找到 transport_agent 根目录的模块
ROOT = Path(__file__).resolve().parent.parent.parent  # /workspace/transport_agent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# 加载 .env（DeepSeek API Key）
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# 初始化协调器（懒加载，避免启动慢）
_coord = None
_transport_agent = None


def get_coordinator():
    global _coord
    if _coord is None:
        from agents.coordinator import Coordinator
        from agents import TransportAgent, BookingAgent, TrackingAgent, build_orchestrator

        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        use_llm = bool(api_key) and not api_key.startswith("sk-your")

        _coord = Coordinator(verbose=False, enable_ocr_preprocess=True)
        _coord.register("transport", TransportAgent(month=11, use_llm=use_llm, verbose=False))
        _coord.register("booking", BookingAgent())
        # customs 入口由报关编排器承载（内部 6 个能力槽位），对外接口不变
        _coord.register("customs", build_orchestrator())
        _coord.register("tracking", TrackingAgent())
    return _coord


def get_transport_agent():
    global _transport_agent
    if _transport_agent is None:
        from agents import TransportAgent
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        use_llm = bool(api_key) and not api_key.startswith("sk-your")
        _transport_agent = TransportAgent(month=11, use_llm=use_llm, verbose=False)
    return _transport_agent


app = FastAPI(title="出海物流多 Agent 系统", version="1.0")

# CORS（前端开发时跨域）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 示例单据静态资源（供前端下载/预览/直接演示）
SCEN_DIR = ROOT / "scenarios"
if SCEN_DIR.exists():
    app.mount("/samples", StaticFiles(directory=str(SCEN_DIR)), name="samples")

# 单据识别器（懒加载）
_reader = None


def get_reader():
    global _reader
    if _reader is None:
        from customs.doc_reader import DocumentReader
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        use_llm = bool(api_key) and not api_key.startswith("sk-your")
        _reader = DocumentReader(use_llm=use_llm, verbose=False)
    return _reader


# 法规知识库（RAG，懒加载：首次调用建索引，仅数十毫秒）
_kb = None


def get_kb():
    global _kb
    if _kb is None:
        from customs.kb import KnowledgeBase
        _kb = KnowledgeBase(ROOT, verbose=False)
    return _kb


# ----------------- 数据模型 -----------------
class DispatchReq(BaseModel):
    query: str
    month: Optional[int] = 11
    interactive: bool = False          # 交互闸门：True 时订舱 Agent 两阶段（出方案→等用户选）
    selected_option: Optional[str] = None  # 阶段2：用户回传所选方案编号（O1/O2…）
    doc_fields: Optional[Dict[str, Any]] = None  # 单据识别结果（阶段2 复用，避免重传文件）


def _build_extra_context(
    interactive: bool,
    selected_option: Optional[str],
    doc_fields: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """组装注入给 Coordinator 的初始 context（交互闸门参数 + 单据要素）。"""
    extra: Dict[str, Any] = {}
    if interactive:
        extra["interactive"] = True
    if selected_option:
        extra["selected_option"] = selected_option
    if doc_fields:
        # 复用单据识别结果，转为下游 Agent 可直接消费的 context
        from customs.doc_reader import DocFields
        df = DocFields(**{k: v for k, v in doc_fields.items()
                          if k in DocFields.__dataclass_fields__})
        extra.update(df.to_context())
    return extra or None


class RoutesReq(BaseModel):
    origin: str
    dest: str
    month: int = 11
    priority: str = "cost"


class KBAskReq(BaseModel):
    question: str
    top_k: int = 5


# ----------------- 接口 -----------------
@app.get("/")
async def index():
    """返回前端首页。"""
    index_path = STATIC_DIR.parent / "index.html"
    if index_path.exists():
        # 禁用 HTML 缓存，确保用户刷新即拿到最新版前端
        return FileResponse(
            str(index_path),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )
    return {"msg": "前端未构建，请访问 /api/docs 查看接口"}


@app.get("/api/health")
async def health():
    """健康检查。"""
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    return {
        "status": "ok",
        "llm_enabled": bool(api_key) and not api_key.startswith("sk-your"),
        "agents": ["transport", "booking", "customs", "tracking"],
        "kb": get_kb().stats(),
    }


@app.get("/api/nodes")
async def get_nodes():
    """返回节点列表（含经纬度，给前端画图用）。"""
    nodes_path = ROOT / "data" / "nodes.json"
    with open(nodes_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data


@app.get("/api/edges")
async def get_edges():
    """返回边列表（给前端画路线用）。"""
    edges_path = ROOT / "data" / "edges.json"
    with open(edges_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data


@app.get("/api/scenarios")
async def get_scenarios():
    """返回内置示例单据清单（含下载/预览 URL），供前端"甩进去即跑"。"""
    index_path = SCEN_DIR / "index.json"
    if not index_path.exists():
        return {"scenarios": []}
    with open(index_path, "r", encoding="utf-8") as f:
        items = json.load(f)
    for it in items:
        it["png_url"] = f"/samples/{it['png']}"
        it["json_url"] = f"/samples/{it['json']}"
    return {"scenarios": items}


@app.get("/api/kb/stats")
async def kb_stats():
    """知识库规模统计（切块数、分词后端、类型分布）。"""
    return get_kb().stats()


@app.get("/api/kb/search")
async def kb_search(
    q: str = Query(..., description="检索词或问题"),
    top_k: int = Query(3, ge=1, le=10),
):
    """知识库检索：返回 Top-K 片段与来源，不做生成。

    结果中 text_kind 标明 原文 / 摘要 / 编者注 / 规则说明，
    前端据此展示引用徽章，避免把摘要当成法条原文。
    """
    if not q.strip():
        raise HTTPException(status_code=400, detail="q 不能为空")
    return get_kb().search(q, top_k=top_k)


@app.post("/api/kb/ask")
async def kb_ask(req: KBAskReq):
    """受约束生成：只依据检索片段作答，强制引用，无依据拒答。

    输出固定标记 ADVISORY：不写 risk_flags、不改 tier、不参与硬校验。
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question 不能为空")
    search = get_kb().search(req.question, top_k=min(max(req.top_k, 1), 10))
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    use_llm = bool(api_key) and not api_key.startswith("sk-your")
    from customs.kb import answer_question
    out = answer_question(req.question, search["hits"], api_key if use_llm else "")
    # 透出检索侧的可解释信息：命中了哪些片段、查询扩展了哪些词
    out["coverage"] = search["coverage"]
    out["expanded_terms"] = search.get("expanded_terms", [])
    out["retrieved"] = len(search["hits"])
    return out


@app.post("/api/recognize_and_dispatch")
async def recognize_and_dispatch(
    file: UploadFile = File(...),
    query: str = Form(""),
    month: int = Form(0),
    interactive: bool = Form(True),
    selected_option: str = Form(""),
):
    """核心入口：甩入单据文件 → AI 识别要素 → 自动跑通全链路。

    - 图片（PNG/JPG）：OCR 提字 → DeepSeek 结构化抽取
    - 表格/JSON：直接解析
    识别出的要素注入 context，query 由识别结果自动生成，无需用户手填。
    """
    try:
        upload_dir = ROOT / "web" / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        img_path = upload_dir / file.filename
        with open(img_path, "wb") as f:
            f.write(await file.read())

        reader = get_reader()
        fields = reader.read(str(img_path))

        # query：优先用户补充，否则由识别要素自动生成
        final_query = query.strip() or fields.to_query()

        coord = get_coordinator()
        from agents import TransportAgent
        use_month = month or fields.month or 11
        for _, agent in coord._agents.items():
            if isinstance(agent, TransportAgent):
                agent.month = use_month

        extra = _build_extra_context(
            interactive, selected_option or None, fields.to_dict()
        )
        result = coord.dispatch(final_query, extra_context=extra)

        return {
            "status": "ok",
            "query": final_query,
            "recognized": fields.to_dict(),
            "dispatch": result.to_dict(),
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"单据识别或调度失败: {e}")


@app.post("/api/dispatch")
async def dispatch(req: DispatchReq):
    """多 Agent 协同：识别意图 → 路由/串联 → 汇总输出。

    interactive=True 时启用订舱交互闸门（阶段1 出候选方案），
    再带 selected_option 调用即进入阶段2（生成议价话术与订舱指令）。
    """
    try:
        coord = get_coordinator()
        # 更新 transport 的月份
        from agents import TransportAgent
        for name, agent in coord._agents.items():
            if isinstance(agent, TransportAgent):
                agent.month = req.month
        extra = _build_extra_context(req.interactive, req.selected_option, req.doc_fields)
        result = coord.dispatch(req.query, extra_context=extra)
        return result.to_dict()
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Agent 执行失败: {e}")


@app.post("/api/routes")
async def get_routes(req: RoutesReq):
    """仅调 transport Agent，返回路线方案。"""
    try:
        agent = get_transport_agent()
        agent.month = req.month
        query = f"{req.origin}到{req.dest}，{req.priority}优先"
        result = agent.ask(query)
        return result.to_dict()
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"路线查询失败: {e}")


@app.post("/api/ocr")
async def ocr_upload(file: UploadFile = File(...)):
    """上传图片，OCR 抽取商品信息。"""
    try:
        # 保存到临时文件
        upload_dir = ROOT / "web" / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        img_path = upload_dir / file.filename
        with open(img_path, "wb") as f:
            content = await file.read()
            f.write(content)

        # 调 OCR
        from customs.ocr import OCRExtractor
        use_vlm = bool(os.getenv("DEEPSEEK_API_KEY"))
        extractor = OCRExtractor(use_vlm=use_vlm, verbose=False)
        cargo = extractor.extract(str(img_path))
        return {
            "status": "ok",
            "image_path": str(img_path),
            "cargo_info": cargo.to_dict(),
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"OCR 失败: {e}")


@app.post("/api/dispatch_image")
async def dispatch_image(
    file: UploadFile = File(...),
    query: str = Form(""),
    month: int = Form(11),
    interactive: bool = Form(False),
    selected_option: str = Form(""),
):
    """上传发票图片 + 文字需求 → 协调器 OCR 预处理 + 多 Agent 协同。"""
    try:
        # 保存图片
        upload_dir = ROOT / "web" / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        img_path = upload_dir / file.filename
        with open(img_path, "wb") as f:
            content = await file.read()
            f.write(content)

        # 拼成 query：图片路径 + 文字需求
        full_query = str(img_path)
        if query:
            full_query = f"{query}（发票见 {img_path}）"

        coord = get_coordinator()
        # 更新 transport 的月份
        from agents import TransportAgent
        for name, agent in coord._agents.items():
            if isinstance(agent, TransportAgent):
                agent.month = month
        extra = _build_extra_context(interactive, selected_option)
        result = coord.dispatch(full_query, extra_context=extra)
        return result.to_dict()
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"图片调度失败: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
