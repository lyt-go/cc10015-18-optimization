"""多维下钻 分析看板接口：汇总指标、参数运行与结果导出。

V018 层级聚合：下钻结果导入（上传文件）、层级聚合明细表、父级汇总核对、
冲突清单、批次留档与下载包核对都挂在 /hierarchy 下，声明在 /{entry_id} 之前。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.drill_down import DrillDownService
from app.services.drill_hierarchy import SCHEMA_VERSION, DrillHierarchyService

router = APIRouter(prefix="/api/drill_down", tags=["多维下钻"])
service = DrillDownService()
hierarchy_service = DrillHierarchyService()
LIST_FIELDS = ["维度编号", "维度名称", "下钻层级", "基准指标", "父级维度", "子级维度", "数据来源", "刷新间隔"]
STATUSES = ["草稿", "待校验", "已生效", "已停用"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    """工作台面板：指标卡与层级聚合明细表同源，和详情页看到的是同一份结果。"""
    panel = hierarchy_service.panel()
    return {"metrics": [
        {"label": "聚合父级数", "value": panel["父级数"], "trend": panel["批次数"]},
        {"label": "明细行数", "value": panel["明细行数"], "trend": 0 if panel["行数核对一致"] else -1},
        {"label": "冲突与回滚", "value": panel["冲突行数"] + panel["回滚批次数"], "trend": -panel["回滚批次数"]},
    ]}


@router.get("")
def list_results() -> dict[str, Any]:
    return {"items": service.list_results()}


@router.post("/run")
def run_analysis(payload: EntryPayload) -> dict[str, Any]:
    results = service.run_analysis(payload.values)
    return {"items": results}


# --------------------------------------------------------------- V018 层级聚合


@router.post("/hierarchy/import")
async def import_hierarchy(file: UploadFile = File(...)) -> dict[str, Any]:
    """上传下钻结果 CSV：按父级到子级关联聚合，父级编号与子级记录同事务落库。"""
    content = await file.read()
    if not content:
        return {"ok": False, "message": "上传文件为空，未写入任何路径"}
    return hierarchy_service.import_file(file.filename or "upload.csv", content)


@router.get("/hierarchy/detail")
def hierarchy_detail() -> dict[str, Any]:
    """层级聚合明细表：每个子级只保留一条当前层级路径。"""
    items = hierarchy_service.detail_rows()
    return {"items": items, "total": len(items), "版本": SCHEMA_VERSION}


@router.get("/hierarchy/summary")
def hierarchy_summary() -> dict[str, Any]:
    """父级汇总 + 父子行数核对，汇总与子级明细行数必须对得上。"""
    return {
        "items": hierarchy_service.summary_rows(),
        "核对": hierarchy_service.reconcile(),
        "版本": SCHEMA_VERSION,
    }


@router.get("/hierarchy/conflicts")
def hierarchy_conflicts() -> dict[str, Any]:
    """冲突清单：同一子级命中两个父级路径时，保留首个有效父级并在此列出。"""
    items = hierarchy_service.conflict_rows()
    return {"items": items, "total": len(items), "版本": SCHEMA_VERSION}


@router.get("/hierarchy/batches")
def hierarchy_batches() -> dict[str, Any]:
    """导入批次留档：已提交/已回滚都按 V018 留档，供事后核对。"""
    items = hierarchy_service.batch_rows()
    return {"items": items, "total": len(items), "版本": SCHEMA_VERSION}


@router.get("/hierarchy/panel")
def hierarchy_panel() -> dict[str, Any]:
    """层级聚合工作台面板，与详情页共用同一份存储结果。"""
    return hierarchy_service.panel()


@router.get("/hierarchy/manifest")
def hierarchy_manifest() -> dict[str, Any]:
    """下载包清单（JSON）：版本、行数、sha256 与父子行数核对。"""
    return hierarchy_service.build_manifest()


@router.get("/hierarchy/package")
def hierarchy_package() -> Response:
    """下载包：全份 V018 层级聚合明细 CSV + manifest，由接收方获取并核对。"""
    payload = hierarchy_service.build_package()
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="hierarchy_package_{SCHEMA_VERSION}.zip"'},
    )


@router.post("/hierarchy/verify")
async def verify_hierarchy_package(file: UploadFile = File(...)) -> dict[str, Any]:
    """接收方核对下载包：重算 sha256、比对行数与库内全份 V018 明细。"""
    content = await file.read()
    if not content:
        return {"ok": False, "message": "上传的下载包为空", "checks": []}
    return hierarchy_service.verify_package(file.filename or "package.zip", content)


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.list_results()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        all_keys = list(items[0].keys())
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=drill_down.csv"})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    """维度详情：附带该维度作为父级的 V018 聚合切片，与面板同一结果。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"下钻维度 {entry_id} 不存在")
    result = dict(entry)
    result["层级聚合"] = hierarchy_service.entry_slice(str(entry.get("维度编号", "")))
    return result
