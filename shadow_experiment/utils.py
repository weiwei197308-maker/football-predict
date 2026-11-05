"""影子实验统计模块 — 工具函数。"""
import hashlib
import json
import os


def sha256_file(path):
    """计算文件 SHA256。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data):
    """计算 bytes/str 的 SHA256（用于 JSON 可重复性验证）。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def load_json(path):
    """安全加载 JSON 文件。返回 (obj, error_str)。"""
    if not os.path.exists(path):
        return None, f"文件不存在: {path}"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except (json.JSONDecodeError, OSError) as e:
        return None, f"JSON 解析失败: {e}"


def save_json(path, obj):
    """原子写入 JSON（先写 .tmp 再 rename）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_csv(path, rows, header=None):
    """简单 CSV 写入（字段含逗号/引号时用 JSON 序列化兜底）。"""
    import csv
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not rows:
        header = header or []
    elif header is None:
        header = list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow([r.get(h, "") for h in header])


def fmt_pct(num, den):
    """安全百分比格式化。分母为 0 时返回 None。"""
    if den is None or den == 0:
        return None
    return round(100.0 * num / den, 2)


def fmt(num, digits=2):
    """安全数字格式化。None 返回 "—"。"""
    if num is None:
        return "—"
    if isinstance(num, float):
        return round(num, digits)
    return num
