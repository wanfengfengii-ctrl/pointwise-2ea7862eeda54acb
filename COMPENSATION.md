# 换装表整窗补偿签报

班组在试车前只能对换装表末值加一次补偿。整张设定窗使用同一个量，补偿单位与两表读数单位相同。许可、读数关系和支持域来自指定的实收件。原登记、逐级试算和固定容差比对见 README.md。

## 冻结和接收许可

扩展 `station.revisions.import_revision(store, instrument_id, evidence, stages, supersedes=None, *, compensation_range=None)`。许可用两个有理端点组成的列表或元组，按下界、上界排列。数值沿用原有理数输入约定，负数及相等端点合法。省略或传 `None` 表示这份修订没有许可。勘误单独声明本版许可，前版许可和前版来件保持原值。非法许可抛出 `InvalidRevision`，整次冻结无新增。原台架试算仍返回原换算值。

沿用 `exchange.issue.issue` 和 `exchange.receive.receive`。有许可的修订发行 V2，无许可的修订发行原 V1。V2 沿用 README.md 所述外层四个字段，将 `version` 设为整数 `2`，以相同规则计算传送摘要。V2 的 JSON 正文仍有 `support` 和 `terminal`，并且恰多出 `compensation_range`，值为两个有理数字符串构成的闭区间。原级序、级参数和原记录仍留站。接收时按版本校验外层、表号、支持域、末值关系和许可，摘要须覆盖实际接到的完整正文。拒绝混用版本字段、缺少或非法的 V2 许可和其他坏件，抛出 `RejectedPiece`，实收账无新增。成功后可按实收号取得该件许可，V1 件的许可为缺失。原逐点 `sign` 继续使用两种来件的支持域及末值。

## 核定和领取

新增 Python 入口 `review.compensation.sign_compensation(receipts, reports, left_receipt, right_receipt, lower, upper, tolerance)`，返回 `reports.record.SignedReport`。实收号顺序、同中心库、同单位、闭窗及容差沿用 `review.compare.sign` 的请求规则，设定窗可为单点。未知实收号及非法请求抛出 `InvalidComparisonRequest`。SQLite 签存错误向调用方传播，报告和成功回执一并撤销。

只要右件无许可，或闭窗内任一点缺少任一来件的支持，就签 `missing_evidence`。证据齐备时，核定许可内所有满足 `abs(左末值(x) - (右末值(x) + 补偿量)) <= tolerance` 的实数补偿量，同一补偿量须对闭窗内每个 `x` 都成立。非空签 `allowed`，保留闭端和单点。证据齐备但空集签 `no_feasible_offset`。

用原 `get_report` 和 `export_report` 按号复领，`decode_report` 可读导出的两种报告。新报告 JSON 保留原格式的 `request`、有序 `origins` 和 `receipt`，用 `kind`（值为 `window_compensation`）和 `compensation` 取代 `partition`。`compensation` 含 `status` 和 `range`。`allowed` 的 `range` 为 `[下界, 上界]`，两端使用原报告的有理字符串或二次根编码，两端相等时仍保留两项。其余状态的 `range` 为 `null`。签好的请求、出处、结果和回执随原号读出，领取时不重新核定。保留旧库、旧实收、旧报告和旧导出格式。

## 试车样例和运行入口

`samples/compensation.json` 的 `instruments` 和 `revisions` 沿用原演示的输入写法。`key` 供样例引用，`supersedes` 指同一样例中的前一修订键，`compensation_range` 对应新增导入参数。`cases` 指定左右修订、闭窗、容差及预期 `compensation` 对象。先实收后移开站库再核定。`interior_extremum` 的上界为 `2√2+1/4`。勘误版沿用原换算关系，但许可不足，另签为无整窗补偿。重开中心库后，初版报告仍可凭初版报告号领取。

使用现有 `.pointwise-runtime.json`，保留清单版本 1 和 `cli` 类型。`setup` 为准备命令参数数组的列表，可为空。将 `verify` 和 `demo` 的命令参数数组接到业务入口。核验入口保留原回归，并检查整窗结果、许可勘误、原号复领与拒写，失败非零退出。演示输出关站后的整窗签报及勘误后按号复领的实值。
