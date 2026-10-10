"""按数据集、大小、定价模式和算法统计成功率及执行时间（均值 ± 样本标准差）。

输入目录：results/数据集名/runtime_summary_大小.csv
运行示例：
  python summarize_runtime_results.py --results-dir ./results
  python summarize_runtime_results.py --results-dir ./results --datasets Adolena Deep100 --price-mode uniform
  python summarize_runtime_results.py --results-dir ./results --price-mode all --queries Q1 Q2

默认读取所有数据集，small/medium/large/huge，Q1-Q5，Uniform，四种算法。
成功率 = sum(n_exact) / sum(n_measured)，排除预热；成功状态沿用实验原有时限。
每个失败运行（n_measured - n_exact）按 --failure-seconds 计时，默认 100 秒。
这个参数只用于失败计时，不改变成功判定，也不把成功时间截断为 100 秒。
先用成功样本的 n/mean/stddev 恢复 M2，再合并失败常数及不同 Query 的样本。
源 stddev_seconds 默认按样本标准差解释；--input-std-ddof 0 可处理总体标准差输入。
所有输出的“±”均为合并后的样本标准差（ddof=1），不是标准误或置信区间。
缺失文件/记录不推定为失败；不完整汇总组的统计值留空。零样本的成功率留空，
单样本的样本标准差留空。全失败且至少两次测量时，均值±标准差为 100±0 秒。

输出：逐 Query CSV、按算法汇总 CSV、跨算法总体汇总 CSV、可读 Markdown 报告。
只依赖 Python 标准库；不修改 CSV 源数据，也不依赖绘图脚本。
"""
import argparse
import csv
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


SIZES = ("small", "medium", "large", "huge")
QUERIES = tuple(f"Q{i}" for i in range(1, 6))
PRICE_MODES = ("uniform", "random", "skewed", "heavy-tailed")
ALGORITHMS = ("Baseline", "KCPrice", "SATPrice", "SATPrice-NoReduction")
FAILURE_FIELDS = ("n_timeout", "n_resource_limit", "n_error", "n_interrupted",
                  "n_unsat", "n_mismatch", "n_other")
OUTPUT_DIRECTORY = Path(__file__).resolve().parent / "execution_statistics"
METRICS = ("n_measured", "n_success", "n_failed", "n_timeout", "n_other_failures",
           "success_rate", "success_rate_pct", "mean_seconds", "stddev_seconds",
           "mean_pm_std_seconds")


@dataclass(frozen=True)
class Moments:
    n: int = 0
    mean: float = 0.0
    m2: float = 0.0  # sum((x - mean)**2)

    def merge(self, other):
        """Chan 合并公式：保留组内和组间变异，避免平均组均值/标准差。"""
        if not self.n:
            return other
        if not other.n:
            return self
        n = self.n + other.n
        delta = other.mean - self.mean
        return Moments(n, self.mean + delta * other.n / n,
                       self.m2 + other.m2 + delta * delta * self.n * other.n / n)

    @property
    def sd(self):
        return math.sqrt(max(0.0, self.m2) / (self.n - 1)) if self.n > 1 else None


def missing(value):
    return value is None or str(value).strip().lower() in {"", "nan", "na", "n/a", "null", "none"}


def count(row, key, context):
    try:
        value = int(row[key])
    except (ValueError, KeyError, TypeError):
        raise ValueError(f"{context}: {key} 必须是非负整数。") from None
    if value < 0:
        raise ValueError(f"{context}: {key} 不能为负数。")
    return value


def number(value, label, context):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{context}: {label} 必须是有限非负数。") from None
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{context}: {label} 必须是有限非负数。")
    return result


def price_mode_of(row, context):
    mode = (row.get("price_mode") or "").strip().lower()
    if mode:
        return mode
    policy = (row.get("price_policy") or "").strip().lower()
    if policy == "uniform_configured":
        return "uniform"
    for candidate in PRICE_MODES:
        if policy == f"tuple-prices-v1:{candidate}":
            return candidate
    raise ValueError(f"{context}: 无法从 price_mode/price_policy 识别定价模式。")


def format_time(mean, sd):
    if mean is None:
        return ""
    return f"{mean:.6f} ± {sd:.6f}" if sd is not None else f"{mean:.6f} ± N/A"


def metrics(moments, success, timeout):
    n = moments.n
    mean = moments.mean if n else None
    rate = success / n if n else None
    return {
        "n_measured": n, "n_success": success, "n_failed": n - success,
        "n_timeout": timeout, "n_other_failures": n - success - timeout,
        "success_rate": rate, "success_rate_pct": rate * 100 if rate is not None else None,
        "mean_seconds": mean, "stddev_seconds": moments.sd,
        "mean_pm_std_seconds": format_time(mean, moments.sd),
    }


def parse_measurement(row, failure_seconds, input_ddof, context):
    n = count(row, "n_measured", context)
    success = count(row, "n_exact", context)
    samples = count(row, "n_time_samples", context)
    timeout = count(row, "n_timeout", context)
    if success > n or timeout > n - success:
        raise ValueError(f"{context}: 成功或超时计数超过正式测量次数。")
    if samples != success:
        raise ValueError(f"{context}: n_time_samples={samples} 与 n_exact={success} 不一致，"
                         "无法用现有统计量还原成功样本的时间。")
    known_failures = [count(row, field, context) for field in FAILURE_FIELDS if field in row]
    if sum(known_failures) > n - success or (
            all(field in row for field in FAILURE_FIELDS) and sum(known_failures) != n - success):
        raise ValueError(f"{context}: 各状态计数与 n_measured 不一致。")
    if "n_total" in row and "n_warmup" in row:
        if count(row, "n_total", context) != n + count(row, "n_warmup", context):
            raise ValueError(f"{context}: n_total 不等于 n_measured + n_warmup。")
    original_mean = original_sd = None
    successful = Moments()
    if success:
        original_mean = number(row.get("mean_seconds"), "mean_seconds", context)
        if success == 1 and missing(row.get("stddev_seconds")):
            original_sd = None
        else:
            original_sd = number(row.get("stddev_seconds"), "stddev_seconds", context)
            if success == 1 and original_sd != 0:
                raise ValueError(f"{context}: 一个成功样本的标准差只能为空或为零。")
        m2 = (success - input_ddof) * (original_sd or 0.0) ** 2
        successful = Moments(success, original_mean, m2)
    penalized = successful.merge(Moments(n - success, failure_seconds, 0.0))
    return {**metrics(penalized, success, timeout),
            "success_mean_seconds": original_mean, "success_stddev_seconds": original_sd,
            "_moments": penalized}


def read_cells(root, dataset, size, modes, queries, failure_seconds, input_ddof):
    """返回一个文件预期的所有组合；缺失数据显式占位，绝不伪造次数。"""
    path = root / dataset / f"runtime_summary_{size}.csv"
    selected, present_modes = {}, set()
    if path.is_file():
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"scenario", "data_size", "query", "algorithm", "n_measured", "n_exact",
                        "n_time_samples", "n_timeout", "mean_seconds", "stddev_seconds"}
            absent = required - set(reader.fieldnames or [])
            if absent:
                raise ValueError(f"{path}: 缺少字段 {sorted(absent)}")
            for line, row in enumerate(reader, 2):
                context = f"{path}:{line}"
                scenario = row["scenario"].strip().replace("\\", "/").rstrip("/")
                if scenario.rsplit("/", 1)[-1].casefold() != dataset.casefold():
                    raise ValueError(f"{context}: scenario={scenario!r} 与数据集目录 {dataset!r} 不一致。")
                if row["data_size"].strip().lower() != size:
                    raise ValueError(f"{context}: data_size 与文件名中的 {size} 不一致。")
                mode = price_mode_of(row, context)
                present_modes.add(mode)
                query, algorithm = row["query"].strip().upper(), row["algorithm"].strip()
                if mode not in modes or query not in queries or algorithm not in ALGORITHMS:
                    continue
                key = (mode, query, algorithm)
                if key in selected:
                    raise ValueError(f"{context}: 重复组合 {key}，请先选择唯一实验配置。")
                selected[key] = parse_measurement(row, failure_seconds, input_ddof, context)
    cells = []
    for mode in modes:
        for query in queries:
            for algorithm in ALGORITHMS:
                key = (mode, query, algorithm)
                result = selected.get(key)
                if result is not None:
                    status = "complete"
                elif not path.is_file():
                    status = "missing_file"
                elif mode not in present_modes:
                    status = "missing_price_mode"
                else:
                    status = "missing_query_or_algorithm"
                cell = {"dataset": dataset, "data_size": size, "price_mode": mode,
                        "algorithm": algorithm, "query": query, "data_status": status,
                        "failure_seconds": failure_seconds,
                        "source_file": path.relative_to(root).as_posix()}
                cell.update(result or {**dict.fromkeys(METRICS),
                                       "success_mean_seconds": None, "success_stddev_seconds": None})
                cells.append(cell)
    return cells


def aggregate(cells, by_algorithm, failure_seconds):
    keys = ["dataset", "data_size", "price_mode"] + (["algorithm"] if by_algorithm else [])
    groups = defaultdict(list)
    for cell in cells:
        groups[tuple(cell[key] for key in keys)].append(cell)
    result = []
    for key, members in groups.items():
        available = [r for r in members if r["data_status"] == "complete"]
        complete = len(available) == len(members)
        row = dict(zip(keys, key))
        row.update({"queries": ",".join(dict.fromkeys(r["query"] for r in members)),
                    "observed_groups": len(available), "expected_groups": len(members),
                    "data_status": "complete" if complete else "incomplete",
                    "missing_reasons": ",".join(sorted({r["data_status"] for r in members
                                                         if r["data_status"] != "complete"})),
                    "failure_seconds": failure_seconds})
        if complete:
            combined = Moments()
            for member in available:
                combined = combined.merge(member["_moments"])
            row.update(metrics(combined, sum(r["n_success"] for r in available),
                               sum(r["n_timeout"] for r in available)))
        else:
            row.update(dict.fromkeys(METRICS))
        result.append(row)
    return result


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_report(path, algorithm_rows, overall_rows, queries, failure_seconds, input_ddof):
    lines = ["# 执行成功率与运行时间统计", "",
             "成功率 = 正式运行成功次数 / 正式运行总次数（n_exact / n_measured），排除预热。",
             f"成功判定沿用实验原有时限；每个失败运行统一按 {failure_seconds:g} 秒计入时间。",
             "平均时间和样本标准差同时包含成功、失败运行；成功时间保持原值。",
             "不同 Query 按正式运行次数加权合并，同时保留组内与组间变异；不直接平均成功率或标准差。",
             f"所选 Query：{', '.join(queries)}。源标准差 ddof={input_ddof}，输出标准差 ddof=1。",
             "缺失文件/模式/记录不等同于失败，相关汇总显示 N/A；单样本的标准差也显示 N/A。", "",
             "## 按算法汇总", "",
             "| 数据集 | 大小 | 定价模式 | 算法 | 成功/正式运行 | 成功率 | 平均时间 ± 标准差（秒） |",
             "|---|---|---|---|---:|---:|---:|"]
    def table_values(row):
        if row["data_status"] != "complete":
            return "N/A", "N/A", "N/A（数据缺失）"
        ratio = f"{row['n_success']}/{row['n_measured']}"
        rate = f"{row['success_rate_pct']:.2f}%" if row["success_rate_pct"] is not None else "N/A"
        duration = row["mean_pm_std_seconds"] or "N/A"
        return ratio, rate, duration
    for row in algorithm_rows:
        ratio, rate, duration = table_values(row)
        lines.append(f"| {row['dataset']} | {row['data_size']} | {row['price_mode']} | {row['algorithm']} | {ratio} | {rate} | {duration} |")
    lines += ["", "## 数据集与大小总体汇总", "",
              "下表将四个算法的正式运行合并；算法间比较请使用上表。", "",
              "| 数据集 | 大小 | 定价模式 | 成功/正式运行 | 成功率 | 平均时间 ± 标准差（秒） |",
              "|---|---|---|---:|---:|---:|"]
    for row in overall_rows:
        ratio, rate, duration = table_values(row)
        lines.append(f"| {row['dataset']} | {row['data_size']} | {row['price_mode']} | {ratio} | {rate} | {duration} |")
    lines += ["", "## 计算方法", "",
              "成功组：n=k，均值为源 mean_seconds，M2=(k−源ddof)×源标准差²。",
              "失败组：n=n_measured−k，均值为失败计时值，M2=0。",
              "合并 A、B 时：Δ=mean_B−mean_A；n=n_A+n_B；",
              "mean=mean_A+Δ×n_B/n；M2=M2_A+M2_B+Δ²×n_A×n_B/n。",
              "最终样本标准差为 sqrt(M2/(n−1))；n<2 时不可定义。", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Summarize success rates and penalized mean +/- sample SD from runtime summary CSVs.")
    parser.add_argument("--results-dir", type=Path, default=Path("results"))
    parser.add_argument("--datasets", nargs="+", help="Dataset directory names; default: discover all datasets.")
    parser.add_argument("--sizes", nargs="+", default=SIZES, choices=SIZES)
    parser.add_argument("--queries", nargs="+", default=QUERIES, type=str.upper)
    parser.add_argument("--price-mode", default="uniform", type=str.lower, choices=(*PRICE_MODES, "all"))
    parser.add_argument("--failure-seconds", default=100.0, type=float,
                        help="Time assigned to each failed run; does not change success labels (default: 100).")
    parser.add_argument("--input-std-ddof", type=int, choices=(0, 1), default=1)
    parser.add_argument("--output", type=Path, default=OUTPUT_DIRECTORY)
    args = parser.parse_args()
    try:
        root = args.results_dir.expanduser()
        if not root.is_dir():
            raise ValueError(f"结果目录不存在：{root.resolve()}")
        if not math.isfinite(args.failure_seconds) or args.failure_seconds <= 0:
            raise ValueError("failure-seconds 必须是有限正数。")
        datasets = args.datasets or sorted(p.name for p in root.iterdir()
                                            if p.is_dir() and any(p.glob("runtime_summary_*.csv")))
        if not datasets:
            raise ValueError("未发现 数据集/runtime_summary_大小.csv 文件。")
        for values, label in ((datasets, "datasets"), (args.sizes, "sizes"), (args.queries, "queries")):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} 不可重复。")
        for dataset in datasets:
            if dataset in {".", ".."} or "/" in dataset or "\\" in dataset or not (root / dataset).is_dir():
                raise ValueError(f"数据集目录名无效或不存在：{dataset!r}")
        modes = PRICE_MODES if args.price_mode == "all" else (args.price_mode,)
        cells = []
        for dataset in datasets:
            for size in args.sizes:
                cells.extend(read_cells(root, dataset, size, modes, args.queries,
                                        args.failure_seconds, args.input_std_ddof))
        if not any(cell["data_status"] == "complete" for cell in cells):
            raise ValueError("筛选条件下没有任何可统计的记录，请检查目录、大小、Query 和定价模式。")
        by_algorithm = aggregate(cells, True, args.failure_seconds)
        overall = aggregate(cells, False, args.failure_seconds)
        args.output.mkdir(parents=True, exist_ok=True)
        suffix = args.price_mode.replace("-", "_")
        common = ["dataset", "data_size", "price_mode"]
        fields = ["queries", "observed_groups", "expected_groups", "data_status", "missing_reasons", *METRICS, "failure_seconds"]
        write_csv(args.output / f"by_query_{suffix}.csv", cells,
                  common + ["algorithm", "query", "data_status", *METRICS,
                            "success_mean_seconds", "success_stddev_seconds", "failure_seconds", "source_file"])
        write_csv(args.output / f"by_dataset_size_algorithm_{suffix}.csv", by_algorithm,
                  common + ["algorithm"] + fields)
        write_csv(args.output / f"by_dataset_size_{suffix}.csv", overall, common + fields)
        write_report(args.output / f"summary_{suffix}.md", by_algorithm, overall,
                     args.queries, args.failure_seconds, args.input_std_ddof)
        incomplete = sum(row["data_status"] != "complete" for row in by_algorithm)
        print(f"Saved 3 CSV files and summary_{suffix}.md to {args.output.resolve()}")
        print(f"Algorithm groups: {len(by_algorithm)}; incomplete groups shown as N/A: {incomplete}")
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
