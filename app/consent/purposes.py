from __future__ import annotations

# 固定用途编码：新增用途必须显式扩展，旧授权记录不会因为用途目录变化而被沿用。
PURPOSE_COLLECTION = "collection"
PURPOSE_INSTANT_REPORT = "instant_report"
PURPOSE_PRODUCT_IMPROVEMENT = "product_improvement"
PURPOSE_PARTNER_SHARING = "partner_sharing"
PURPOSE_FOLLOW_UP = "follow_up"

PURPOSES: tuple[str, ...] = (
    PURPOSE_COLLECTION,
    PURPOSE_INSTANT_REPORT,
    PURPOSE_PRODUCT_IMPROVEMENT,
    PURPOSE_PARTNER_SHARING,
    PURPOSE_FOLLOW_UP,
)

PURPOSE_LABELS: dict[str, str] = {
    PURPOSE_COLLECTION: "采集体验数据",
    PURPOSE_INSTANT_REPORT: "生成即时结论",
    PURPOSE_PRODUCT_IMPROVEMENT: "内部产品改进",
    PURPOSE_PARTNER_SHARING: "向指定合作方共享",
    PURPOSE_FOLLOW_UP: "后续联系",
}

# 依赖关系：上游用途被拒绝时，下游用途不能被允许。
PURPOSE_REQUIRES: dict[str, tuple[str, ...]] = {
    PURPOSE_COLLECTION: (),
    PURPOSE_INSTANT_REPORT: (PURPOSE_COLLECTION,),
    PURPOSE_PRODUCT_IMPROVEMENT: (PURPOSE_COLLECTION,),
    PURPOSE_PARTNER_SHARING: (PURPOSE_COLLECTION,),
    PURPOSE_FOLLOW_UP: (),
}

# 撤回后派生记录的默认处置队列：按用途来源决定保留、隔离或删除。
DEFAULT_DERIVED_DISPOSITION: dict[str, str] = {
    PURPOSE_COLLECTION: "delete",
    PURPOSE_INSTANT_REPORT: "delete",
    PURPOSE_PRODUCT_IMPROVEMENT: "quarantine",
    PURPOSE_PARTNER_SHARING: "delete",
    PURPOSE_FOLLOW_UP: "retain",
}

RECORD_TYPE_BY_PURPOSE: dict[str, str] = {
    PURPOSE_COLLECTION: "raw_capture",
    PURPOSE_INSTANT_REPORT: "instant_report",
    PURPOSE_PRODUCT_IMPROVEMENT: "improvement_dataset",
    PURPOSE_PARTNER_SHARING: "partner_export",
    PURPOSE_FOLLOW_UP: "follow_up_record",
}
