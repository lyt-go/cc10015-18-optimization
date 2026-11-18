"""V018 层级聚合明细表链路测试：导入聚合、唯一路径、续传、回滚留档、下载包核对与跨模块联动。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.services.drill_hierarchy import (
    BATCH_ROLLED_BACK,
    BATCH_TABLE,
    CONFLICT_TABLE,
    DETAIL_TABLE,
    SCHEMA_VERSION,
    SUMMARY_TABLE,
    DrillHierarchyService,
)
from app.services.data_filter import DataFilterService

TABLES = [DETAIL_TABLE, SUMMARY_TABLE, CONFLICT_TABLE, BATCH_TABLE, "drill_down", "data_filter"]

CSV_HEADER = "父级编号,父级名称,子级编号,子级名称,指标值,筛选规则版本\n"


def _reset():
    for name in TABLES:
        store._tables[name] = []


def _seed_dims_and_rules():
    """造下钻维度定义与已生效筛选规则，模拟既有链路。"""
    store.rows("drill_down").extend([
        {"id": 1, "维度编号": "DRIL-A", "维度名称": "渠道", "下钻层级": "1", "父级维度": "", "子级维度": "子渠道", "status": "已生效"},
        {"id": 2, "维度编号": "DRIL-B", "维度名称": "地区", "下钻层级": "1", "父级维度": "", "子级维度": "城市", "status": "已生效"},
    ])
    store.rows("data_filter").extend([
        {"id": 1, "规则编号": "DATA-0001", "数据来源": "明细", "筛选字段": "渠道", "规则版本": "r1", "status": "已生效"},
        {"id": 2, "规则编号": "DATA-0002", "数据来源": "明细", "筛选字段": "地区", "规则版本": "r2", "status": "草稿"},
    ])


def _csv(*rows: str) -> bytes:
    return (CSV_HEADER + "\n".join(rows) + "\n").encode("utf-8")


def test_import_aggregates_parent_child_with_unique_paths():
    """导入按父级到子级关联聚合，明细表保留唯一路径，父子行数可核对。"""
    _reset()
    _seed_dims_and_rules()
    svc = DrillHierarchyService()
    result = svc.import_file("drill.csv", _csv(
        "DRIL-A,渠道,A-1,子渠道1,0.5,r1",
        "DRIL-A,渠道,A-2,子渠道2,0.7,r1",
        "DRIL-B,地区,B-1,城市1,0.3,r2",
    ))
    assert result["ok"], result
    assert result["写入行数"] == 3
    details = store.rows(DETAIL_TABLE)
    paths = [row["层级路径"] for row in details]
    assert len(paths) == len(set(paths)), "层级聚合明细表必须保留唯一路径"
    # 读取下钻维度补全父级名称
    assert next(r for r in details if r["父级编号"] == "DRIL-A")["父级名称"] == "渠道"
    # 父级汇总与子级明细行数核对
    recon = svc.reconcile()
    assert recon["总计一致"], recon
    assert recon["明细行数合计"] == 3
    summary = {row["父级编号"]: row for row in store.rows(SUMMARY_TABLE)}
    assert summary["DRIL-A"]["子级行数"] == 2
    assert abs(summary["DRIL-A"]["指标合计"] - 1.2) < 1e-6


def test_reimport_resumes_only_missing_paths():
    """重复导入只续写缺失路径，已确认父级不被重复子项覆盖，每个子级只进一条路径。"""
    _reset()
    _seed_dims_and_rules()
    svc = DrillHierarchyService()
    first = svc.import_file("part1.csv", _csv(
        "DRIL-A,渠道,A-1,子渠道1,0.5,r1",
        "DRIL-A,渠道,A-2,子渠道2,0.7,r1",
    ))
    assert first["ok"] and first["写入行数"] == 2
    # 中断后续传：A-2 已确认，只补 A-3
    second = svc.import_file("part2.csv", _csv(
        "DRIL-A,渠道,A-2,子渠道2-改,9.9,r1",
        "DRIL-A,渠道,A-3,子渠道3,0.9,r1",
    ))
    assert second["ok"], second
    assert second["跳过行数"] == 1
    assert second["写入行数"] == 1
    details = store.rows(DETAIL_TABLE)
    assert len(details) == 3
    kept = next(r for r in details if r["子级编号"] == "A-2")
    assert kept["子级名称"] == "子渠道2" and kept["指标值"] == 0.7, "已确认父级不得被重复子项覆盖"
    children = [r["子级编号"] for r in details]
    assert len(children) == len(set(children)), "每个子级只进入一条当前层级路径"
    assert svc.reconcile()["总计一致"]


def test_conflict_keeps_first_parent_and_lists_conflicts():
    """同一子级命中两个父级路径：保留首个有效父级，冲突路径进清单。"""
    _reset()
    _seed_dims_and_rules()
    svc = DrillHierarchyService()
    svc.import_file("first.csv", _csv("DRIL-A,渠道,C-1,共用子级,0.5,r1"))
    result = svc.import_file("second.csv", _csv(
        "DRIL-B,地区,C-1,共用子级,0.8,r2",
        "DRIL-B,地区,B-9,城市9,0.1,r2",
    ))
    assert result["ok"], result
    assert result["冲突行数"] == 1
    details = store.rows(DETAIL_TABLE)
    owners = [r["父级编号"] for r in details if r["子级编号"] == "C-1"]
    assert owners == ["DRIL-A"], "明细表保留唯一归属：首个有效父级"
    conflicts = store.rows(CONFLICT_TABLE)
    assert len(conflicts) == 1
    assert conflicts[0]["冲突路径"] == "DRIL-B/C-1"
    assert conflicts[0]["保留父级"] == "DRIL-A"
    assert conflicts[0]["版本"] == SCHEMA_VERSION
    assert svc.reconcile()["总计一致"]


def test_transaction_anomaly_rolls_back_and_archives():
    """同批出现两条相同路径的相关记录：事务异常，V018 回滚、清残留并留档。"""
    _reset()
    _seed_dims_and_rules()
    svc = DrillHierarchyService()
    result = svc.import_file("bad.csv", _csv(
        "DRIL-A,渠道,A-1,子渠道1,0.5,r1",
        "DRIL-A,渠道,A-1,子渠道1-重复,0.6,r1",
        "DRIL-B,地区,B-1,城市1,0.3,r2",
    ))
    assert not result["ok"]
    assert result["状态"] == BATCH_ROLLED_BACK
    assert "事务异常" in result["message"]
    # 清残留：明细、汇总、冲突都不能有本批次的行
    assert store.rows(DETAIL_TABLE) == []
    assert store.rows(SUMMARY_TABLE) == []
    assert store.rows(CONFLICT_TABLE) == []
    # 按 V018 留档核对
    batches = store.rows(BATCH_TABLE)
    assert len(batches) == 1
    assert batches[0]["状态"] == BATCH_ROLLED_BACK
    assert batches[0]["版本"] == SCHEMA_VERSION
    assert "DRIL-A/A-1" in batches[0]["异常说明"]
    # 回滚批次不占用路径，修正后可重新导入
    retry = svc.import_file("fixed.csv", _csv("DRIL-A,渠道,A-1,子渠道1,0.5,r1"))
    assert retry["ok"] and retry["写入行数"] == 1


def test_panel_and_entry_slice_share_same_result():
    """工作台面板与详情页读同一份层级聚合结果。"""
    _reset()
    _seed_dims_and_rules()
    svc = DrillHierarchyService()
    svc.import_file("drill.csv", _csv(
        "DRIL-A,渠道,A-1,子渠道1,0.5,r1",
        "DRIL-A,渠道,A-2,子渠道2,0.7,r1",
    ))
    panel = svc.panel()
    slice_a = svc.entry_slice("DRIL-A")
    assert panel["明细行数"] == 2
    assert slice_a["明细行数"] == panel["明细行数"]
    assert slice_a["汇总"]["子级行数"] == 2
    assert panel["行数核对一致"]


def test_package_manifest_and_receiver_verify():
    """下载包带 manifest，接收方核对全份 V018 明细；篡改后核对失败。"""
    _reset()
    _seed_dims_and_rules()
    svc = DrillHierarchyService()
    svc.import_file("drill.csv", _csv(
        "DRIL-A,渠道,A-1,子渠道1,0.5,r1",
        "DRIL-B,地区,B-1,城市1,0.3,r2",
    ))
    manifest = svc.build_manifest()
    assert manifest["版本"] == SCHEMA_VERSION
    assert manifest["明细行数"] == 2
    assert manifest["行数核对"]["一致"]
    package = svc.build_package()
    ok = svc.verify_package("hierarchy_package_V018.zip", package)
    assert ok["ok"], ok
    # 篡改包内 CSV 后 sha256 对不上
    import io, json, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hierarchy_detail_V018.csv", svc.build_csv() + "篡改行\n")
        zf.writestr("manifest_V018.json", json.dumps(manifest, ensure_ascii=False))
    bad = svc.verify_package("tampered.zip", buf.getvalue())
    assert not bad["ok"]
    assert any(not c["通过"] for c in bad["checks"])


def test_data_filter_outputs_only_records_matching_parent():
    """跨模块联动：数据筛选读取明细表中的规则版本，只输出满足当前父级条件的记录。"""
    _reset()
    _seed_dims_and_rules()
    svc = DrillHierarchyService()
    svc.import_file("drill.csv", _csv(
        "DRIL-A,渠道,A-1,子渠道1,0.5,r1",
        "DRIL-A,渠道,A-2,子渠道2,0.7,r2",
        "DRIL-B,地区,B-1,城市1,0.3,r1",
    ))
    df = DataFilterService()
    out = df.hierarchy_output("DRIL-A")
    assert out is not None
    assert out["版本"] == SCHEMA_VERSION
    assert out["筛选规则版本"] == ["r1", "r2"]
    # 只有 r1 是已生效规则：只输出满足当前父级条件（DRIL-A 路径下、规则版本命中生效规则）的记录
    assert out["total"] == 1
    assert out["items"][0]["子级编号"] == "A-1"
    assert out["items"][0]["父级编号"] == "DRIL-A"
    assert df.hierarchy_output("DRIL-NONE") is None
