"""多维下钻 层级聚合明细服务（V018）。

下钻结果导入的层级归属、父子关联聚合、唯一路径约束、事务回滚与留档都收在这里：

- 导入时先读取下钻维度（drill_down 模块）与筛选结果数据集（data_filter 已生效规则版本），
  再按父级到子级关联聚合，层级聚合明细表按「父级编号/子级编号」保留唯一路径；
- 父级编号与子级记录在同一事务里一起落库，提交前做完整性校验：同一批次若会让
  明细表生成两条相同路径的相关记录，判定事务异常，整批回滚、清理残留并按 V018 留档；
- 重复导入（含中断后续传）只续写缺失路径，已确认父级不被重复子项覆盖；
  同一子级命中两个父级路径时保留首个有效父级，冲突路径记入冲突清单；
- 下载包（明细 CSV + manifest）带 sha256 与父子行数核对，接收方凭 manifest
  核对全份 V018 层级聚合明细表。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime
from typing import Any

from app.store import store

SCHEMA_VERSION = "V018"
DETAIL_TABLE = "drill_hierarchy_detail"      # 层级聚合明细表
SUMMARY_TABLE = "drill_hierarchy_summary"    # 父级汇总
CONFLICT_TABLE = "drill_hierarchy_conflict"  # 冲突清单
BATCH_TABLE = "drill_import_batch"           # 导入批次留档

IMPORT_COLUMNS = ["父级编号", "父级名称", "子级编号", "子级名称", "指标值", "筛选规则版本"]
REQUIRED_COLUMNS = ["父级编号", "子级编号"]
EXPORT_COLUMNS = ["批次号", "版本", "父级编号", "父级名称", "子级编号", "子级名称", "层级路径", "指标值", "筛选规则版本", "状态"]
DETAIL_STATUS_CONFIRMED = "已确认"
BATCH_COMMITTED = "已提交"
BATCH_ROLLED_BACK = "已回滚"


class DrillHierarchyService:
    """V018 层级聚合：导入、核对、下载包与冲突清单。"""

    # ------------------------------------------------------------------ 导入

    def import_file(self, filename: str, content: bytes) -> dict[str, Any]:
        """上传文件导入下钻结果：父级编号与子级记录同事务落库，异常整批回滚。"""
        batch_no = f"IMP-{SCHEMA_VERSION}-{store.next_id(BATCH_TABLE):04d}"
        text = content.decode("utf-8-sig", errors="replace")
        parsed, rejected = self._parse_csv(text)
        dims = self._dimension_index()
        rule_versions = self._active_rule_versions()

        staged_details: list[dict[str, Any]] = []
        staged_conflicts: list[dict[str, Any]] = []
        skipped = 0
        existing_paths = {str(r.get("层级路径", "")) for r in store.rows(DETAIL_TABLE)}
        child_owner = self._child_owner_index()
        seen_paths: set[str] = set()
        now = datetime.now().isoformat(timespec="seconds")

        for row in parsed:
            parent = row["父级编号"]
            child = row["子级编号"]
            path = f"{parent}/{child}"
            if path in existing_paths:
                # 中断续传/重复导入：已确认路径只跳过，不用重复子项覆盖已确认父级
                skipped += 1
                continue
            owner = child_owner.get(child)
            if owner is not None and owner != parent:
                # 版本冲突：同一子级命中两个父级路径，保留首个有效父级并列出冲突路径
                staged_conflicts.append(self._conflict_row(batch_no, child, owner, parent, now))
                continue
            dim = dims.get(parent, {})
            staged_details.append({
                "id": 0,  # 提交时统一发号
                "批次号": batch_no,
                "版本": SCHEMA_VERSION,
                "父级编号": parent,
                "父级名称": str(dim.get("维度名称") or row.get("父级名称") or parent),
                "子级编号": child,
                "子级名称": row.get("子级名称") or child,
                "层级路径": path,
                "指标值": row["指标值"],
                "筛选规则版本": row.get("筛选规则版本", ""),
                "规则已生效": row.get("筛选规则版本", "") in rule_versions,
                "状态": DETAIL_STATUS_CONFIRMED,
                "导入时间": now,
            })
            child_owner[child] = parent
            seen_paths.add(path)

        # 事务完整性校验：同一批次若让明细表生成两条相同路径的相关记录，判定事务异常
        anomaly = self._detect_path_anomaly(staged_details)
        if anomaly:
            self._clean_residual(batch_no)
            batch = self._archive_batch(
                batch_no, filename, BATCH_ROLLED_BACK, len(parsed), 0, skipped,
                len(staged_conflicts), rejected, anomaly, staged_details,
            )
            return {
                "ok": False,
                "message": f"事务异常：{anomaly}；批次 {batch_no} 已按 {SCHEMA_VERSION} 回滚并清残留",
                "批次号": batch_no,
                "版本": SCHEMA_VERSION,
                "状态": BATCH_ROLLED_BACK,
                "收到行数": len(parsed),
                "写入行数": 0,
                "跳过行数": skipped,
                "冲突行数": 0,
                "拒绝行数": len(rejected),
                "异常说明": anomaly,
                "留档": batch,
            }

        # 提交：父级汇总与子级明细在同一事务里一起落库
        details = store.rows(DETAIL_TABLE)
        next_id = store.next_id(DETAIL_TABLE)
        for item in staged_details:
            item["id"] = next_id
            next_id += 1
            details.append(item)
        store.rows(CONFLICT_TABLE).extend(
            {"id": store.next_id(CONFLICT_TABLE), **row} for row in staged_conflicts
        )
        self._rebuild_summary(batch_no)
        store.bump_version(DETAIL_TABLE)
        store.bump_version(SUMMARY_TABLE)
        batch = self._archive_batch(
            batch_no, filename, BATCH_COMMITTED, len(parsed), len(staged_details),
            skipped, len(staged_conflicts), rejected, None, staged_details,
        )
        return {
            "ok": True,
            "message": (
                f"批次 {batch_no} 已提交：写入 {len(staged_details)} 条路径，"
                f"续传跳过 {skipped} 条，冲突 {len(staged_conflicts)} 条"
            ),
            "批次号": batch_no,
            "版本": SCHEMA_VERSION,
            "状态": BATCH_COMMITTED,
            "收到行数": len(parsed),
            "写入行数": len(staged_details),
            "跳过行数": skipped,
            "冲突行数": len(staged_conflicts),
            "拒绝行数": len(rejected),
            "冲突路径": [row["冲突路径"] for row in staged_conflicts],
            "留档": batch,
        }

    def _parse_csv(self, text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """解析上传 CSV；缺必填列或指标值非数字的行记入拒绝清单，不拖垮整批。"""
        reader = csv.DictReader(io.StringIO(text))
        rows: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for line_no, raw in enumerate(reader, start=2):
            row = {key: str(raw.get(key) or "").strip() for key in IMPORT_COLUMNS}
            missing = [col for col in REQUIRED_COLUMNS if not row[col]]
            if missing:
                rejected.append({"行号": line_no, "原因": f"缺少必填列：{'、'.join(missing)}", "内容": dict(raw)})
                continue
            try:
                row["指标值"] = round(float(row["指标值"] or 0), 3)
            except ValueError:
                rejected.append({"行号": line_no, "原因": f"指标值「{row['指标值']}」不是数字", "内容": dict(raw)})
                continue
            rows.append(row)
        return rows, rejected

    def _dimension_index(self) -> dict[str, dict[str, Any]]:
        """读取下钻维度定义，按维度编号建索引，用于补全父级名称与层级归属。"""
        return {
            str(row.get("维度编号", "")): row
            for row in store.rows("drill_down")
            if row.get("维度编号")
        }

    def _active_rule_versions(self) -> set[str]:
        """读取筛选结果数据集：data_filter 已生效规则的规则版本集合。"""
        return {
            str(row.get("规则版本", ""))
            for row in store.rows("data_filter")
            if row.get("status") == "已生效" and str(row.get("规则版本", "")).strip()
        }

    def _child_owner_index(self) -> dict[str, str]:
        """已确认明细中子级 → 父级 的归属索引，保证每个子级只进入一条当前层级路径。"""
        owner: dict[str, str] = {}
        for row in store.rows(DETAIL_TABLE):
            if row.get("状态") == DETAIL_STATUS_CONFIRMED:
                owner.setdefault(str(row.get("子级编号", "")), str(row.get("父级编号", "")))
        return owner

    def _conflict_row(self, batch_no: str, child: str, kept: str, dropped: str, now: str) -> dict[str, Any]:
        return {
            "批次号": batch_no,
            "版本": SCHEMA_VERSION,
            "子级编号": child,
            "保留父级": kept,
            "冲突父级": dropped,
            "冲突路径": f"{dropped}/{child}",
            "判定": "保留首个有效父级",
            "时间": now,
        }

    def _detect_path_anomaly(self, staged: list[dict[str, Any]]) -> str | None:
        """检测本批次是否会让明细表生成两条相同路径的相关记录（事务异常）。"""
        seen: set[str] = set()
        for row in staged:
            path = str(row["层级路径"])
            if path in seen:
                return f"明细表将为路径「{path}」生成两条相关记录"
            seen.add(path)
        return None

    def _clean_residual(self, batch_no: str) -> None:
        """回滚后清残留：删掉本批次已写入明细/汇总/冲突的任何行。"""
        for table in (DETAIL_TABLE, SUMMARY_TABLE, CONFLICT_TABLE):
            rows = store.rows(table)
            rows[:] = [row for row in rows if row.get("批次号") != batch_no]

    def _rebuild_summary(self, batch_no: str) -> None:
        """按已确认明细全量重算父级汇总，保证父级汇总与子级明细行数可核对。"""
        details = [r for r in store.rows(DETAIL_TABLE) if r.get("状态") == DETAIL_STATUS_CONFIRMED]
        grouped: dict[str, dict[str, Any]] = {}
        for row in details:
            parent = str(row.get("父级编号", ""))
            bucket = grouped.setdefault(parent, {
                "父级编号": parent,
                "父级名称": str(row.get("父级名称", parent)),
                "子级行数": 0,
                "指标合计": 0.0,
            })
            bucket["子级行数"] += 1
            bucket["指标合计"] = round(bucket["指标合计"] + float(row.get("指标值", 0) or 0), 3)
        summary = store.rows(SUMMARY_TABLE)
        summary[:] = [
            {
                "id": index + 1,
                "批次号": batch_no,
                "版本": SCHEMA_VERSION,
                "状态": DETAIL_STATUS_CONFIRMED,
                **bucket,
            }
            for index, (_, bucket) in enumerate(sorted(grouped.items()))
        ]

    def _archive_batch(
        self,
        batch_no: str,
        filename: str,
        status: str,
        received: int,
        written: int,
        skipped: int,
        conflicts: int,
        rejected: list[dict[str, Any]],
        anomaly: str | None,
        staged: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """按 V018 留档：批次状态、行数口径与路径快照，供事后核对。"""
        batch = {
            "id": store.next_id(BATCH_TABLE),
            "批次号": batch_no,
            "版本": SCHEMA_VERSION,
            "来源文件": filename,
            "状态": status,
            "收到行数": received,
            "写入行数": written,
            "跳过行数": skipped,
            "冲突行数": conflicts,
            "拒绝行数": len(rejected),
            "异常说明": anomaly or "",
            "留档快照": {
                "路径": [row["层级路径"] for row in staged],
                "拒绝行": rejected,
            },
            "时间": datetime.now().isoformat(timespec="seconds"),
        }
        store.rows(BATCH_TABLE).append(batch)
        store.bump_version(BATCH_TABLE)
        return batch

    # ------------------------------------------------------------------ 查询

    def detail_rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(DETAIL_TABLE)]

    def summary_rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(SUMMARY_TABLE)]

    def conflict_rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(CONFLICT_TABLE)]

    def batch_rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(BATCH_TABLE)]

    def reconcile(self) -> dict[str, Any]:
        """父级汇总与子级明细行数核对：逐父级比对，再给总计口径。"""
        details = [r for r in store.rows(DETAIL_TABLE) if r.get("状态") == DETAIL_STATUS_CONFIRMED]
        per_parent: dict[str, int] = {}
        for row in details:
            parent = str(row.get("父级编号", ""))
            per_parent[parent] = per_parent.get(parent, 0) + 1
        checks = []
        consistent = True
        for summary in store.rows(SUMMARY_TABLE):
            parent = str(summary.get("父级编号", ""))
            detail_count = per_parent.get(parent, 0)
            match = int(summary.get("子级行数", 0)) == detail_count
            consistent = consistent and match
            checks.append({
                "父级编号": parent,
                "汇总行数": int(summary.get("子级行数", 0)),
                "明细行数": detail_count,
                "一致": match,
            })
        summary_total = sum(int(s.get("子级行数", 0)) for s in store.rows(SUMMARY_TABLE))
        detail_total = len(details)
        total_match = summary_total == detail_total and len(checks) == len(per_parent)
        return {
            "版本": SCHEMA_VERSION,
            "父级核对": checks,
            "汇总行数合计": summary_total,
            "明细行数合计": detail_total,
            "逐父级一致": consistent,
            "总计一致": consistent and total_match,
        }

    def panel(self) -> dict[str, Any]:
        """工作台面板：与详情页读同一份层级聚合存储，保证两处结果一致。"""
        reconcile = self.reconcile()
        batches = store.rows(BATCH_TABLE)
        return {
            "版本": SCHEMA_VERSION,
            "父级数": len(store.rows(SUMMARY_TABLE)),
            "明细行数": reconcile["明细行数合计"],
            "冲突行数": len(store.rows(CONFLICT_TABLE)),
            "批次数": len(batches),
            "回滚批次数": sum(1 for b in batches if b.get("状态") == BATCH_ROLLED_BACK),
            "行数核对一致": reconcile["总计一致"],
        }

    def entry_slice(self, dimension_code: str) -> dict[str, Any]:
        """详情页切片：某个维度作为父级时的聚合结果，与面板同源。"""
        summary = next(
            (dict(s) for s in store.rows(SUMMARY_TABLE) if s.get("父级编号") == dimension_code),
            None,
        )
        details = [
            dict(r) for r in store.rows(DETAIL_TABLE)
            if r.get("父级编号") == dimension_code and r.get("状态") == DETAIL_STATUS_CONFIRMED
        ]
        return {
            "版本": SCHEMA_VERSION,
            "父级编号": dimension_code,
            "汇总": summary,
            "明细行数": len(details),
            "明细": details,
        }

    # ------------------------------------------------------------------ 下载包

    def build_csv(self) -> str:
        """全份 V018 层级聚合明细表 CSV，列序固定，供下载包与再次导入核对。"""
        buf = io.StringIO(newline="")
        buf.write("\ufeff")
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(EXPORT_COLUMNS)
        for row in store.rows(DETAIL_TABLE):
            writer.writerow([row.get(col, "") for col in EXPORT_COLUMNS])
        return buf.getvalue()

    def build_manifest(self) -> dict[str, Any]:
        """下载包清单：版本、行数、sha256 与父子行数核对，接收方据此核对。"""
        csv_text = self.build_csv()
        reconcile = self.reconcile()
        return {
            "版本": SCHEMA_VERSION,
            "生成时间": datetime.now().isoformat(timespec="seconds"),
            "明细行数": len(store.rows(DETAIL_TABLE)),
            "父级数": len(store.rows(SUMMARY_TABLE)),
            "冲突行数": len(store.rows(CONFLICT_TABLE)),
            "文件清单": [{
                "文件名": f"hierarchy_detail_{SCHEMA_VERSION}.csv",
                "sha256": hashlib.sha256(csv_text.encode("utf-8")).hexdigest(),
                "行数": len(store.rows(DETAIL_TABLE)),
            }],
            "行数核对": {
                "汇总行数合计": reconcile["汇总行数合计"],
                "明细行数合计": reconcile["明细行数合计"],
                "一致": reconcile["总计一致"],
            },
        }

    def build_package(self) -> bytes:
        """下载包：明细 CSV + manifest.json 打成 zip，由接收方获取全份 V018 明细。"""
        manifest = self.build_manifest()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"hierarchy_detail_{SCHEMA_VERSION}.csv", self.build_csv())
            zf.writestr(f"manifest_{SCHEMA_VERSION}.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return buf.getvalue()

    def verify_package(self, filename: str, content: bytes) -> dict[str, Any]:
        """接收方核对下载包：重算 sha256、比对行数与库内全份 V018 明细。"""
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"核对项": name, "通过": ok, "说明": detail})

        csv_text: str | None = None
        csv_bytes: bytes | None = None
        manifest: dict[str, Any] | None = None
        if filename.lower().endswith(".zip") or content[:2] == b"PK":
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as zf:
                    names = set(zf.namelist())
                    csv_name = f"hierarchy_detail_{SCHEMA_VERSION}.csv"
                    manifest_name = f"manifest_{SCHEMA_VERSION}.json"
                    if csv_name not in names or manifest_name not in names:
                        add("包结构", False, f"包内缺少 {csv_name} 或 {manifest_name}，实际文件：{sorted(names)}")
                        return {"ok": False, "message": "下载包结构不完整", "checks": checks}
                    csv_bytes = zf.read(csv_name)
                    csv_text = csv_bytes.decode("utf-8-sig", errors="replace")
                    manifest = json.loads(zf.read(manifest_name).decode("utf-8"))
                add("包结构", True, "CSV 与 manifest 齐全")
            except (zipfile.BadZipFile, ValueError) as exc:
                add("包结构", False, f"下载包无法解析：{exc}")
                return {"ok": False, "message": "下载包无法解析", "checks": checks}
        else:
            csv_bytes = content
            csv_text = content.decode("utf-8-sig", errors="replace")
            add("包结构", True, "收到裸 CSV，跳过 manifest 比对")

        rows = list(csv.reader(io.StringIO(csv_text)))
        data_rows = max(len(rows) - 1, 0)
        if manifest is not None:
            version_ok = manifest.get("版本") == SCHEMA_VERSION
            add("版本", version_ok, f"manifest 版本={manifest.get('版本')}，期望 {SCHEMA_VERSION}")
            expected = manifest.get("文件清单", [{}])[0]
            digest = hashlib.sha256(csv_bytes).hexdigest()
            add("sha256", digest == expected.get("sha256"), f"重算={digest[:12]}…，清单={str(expected.get('sha256'))[:12]}…")
            add("包内行数", data_rows == int(expected.get("行数", -1)), f"CSV 明细 {data_rows} 行，清单 {expected.get('行数')} 行")
        current = len(store.rows(DETAIL_TABLE))
        add("全份行数", data_rows == current, f"下载包明细 {data_rows} 行，库内 {SCHEMA_VERSION} 明细 {current} 行")
        ok = all(item["通过"] for item in checks)
        return {
            "ok": ok,
            "message": "下载包核对通过，接收方已获取全份 V018 层级聚合明细表" if ok else "下载包核对未通过，请重新获取",
            "checks": checks,
        }
