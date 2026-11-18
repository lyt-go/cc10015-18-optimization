"""数据筛选业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.drill_hierarchy import DETAIL_STATUS_CONFIRMED, DETAIL_TABLE, SCHEMA_VERSION
from app.store import store

MODULE = "data_filter"
LIST_FIELDS = ["规则编号", "数据来源", "筛选字段", "筛选算子", "筛选阈值", "逻辑组合", "输出集合", "规则版本"]
REQUIRED_FIELDS = ["规则编号", "数据来源", "筛选字段"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
NEGATIVE_ACTIONS = ["停用规则"]


class DataFilterService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("规则编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def hierarchy_output(self, parent_code: str) -> dict[str, Any] | None:
        """跨模块联动（V018）：读取层级聚合明细表中的筛选规则版本，只输出满足当前父级条件的记录。"""
        details = [
            row for row in store.rows(DETAIL_TABLE)
            if row.get("父级编号") == parent_code and row.get("状态") == DETAIL_STATUS_CONFIRMED
        ]
        if not details:
            return None
        versions = sorted({str(row.get("筛选规则版本", "")) for row in details if row.get("筛选规则版本")})
        rules = [
            dict(row) for row in store.rows(MODULE)
            if str(row.get("规则版本", "")) in versions
        ]
        active_versions = {str(row.get("规则版本", "")) for row in rules if row.get("status") == "已生效"}
        records = [
            dict(row) for row in details
            if str(row.get("筛选规则版本", "")) in active_versions
        ]
        return {
            "版本": SCHEMA_VERSION,
            "父级编号": parent_code,
            "筛选规则版本": versions,
            "生效规则版本": sorted(active_versions),
            "命中规则": rules,
            "items": records,
            "total": len(records),
        }

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"筛选规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据筛选可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"筛选规则已{action}"
