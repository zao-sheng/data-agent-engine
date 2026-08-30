"""样例数据生成器：order 主题 15 张表，固定随机种子可复现。

流程：
  1. 生成 4 张 DIM 维度表（固定种子）
  2. 生成 4 张 DWD 明细事实表（近 90 天，dt='YYYYMMDD' 分区；is_valid 留 10% 无效）
  3. 用 SQL 从 DWD 聚合生成 4 张 DWS 日汇总 + 3 张 ADS 应用汇总
     —— dws/ads 与 dwd 数据严格一致（模拟真实 ETL），评测可交叉验证

用法：
  python -m seed.seed [--out backend/seed/sample.db] [--seed 42] [--days 90]
"""
from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

REGIONS = ["华东", "华南", "华北", "西南", "华中"]
CITIES_BY_REGION = {
    "华东": ["上海", "杭州", "南京", "苏州"],
    "华南": ["广州", "深圳", "厦门"],
    "华北": ["北京", "天津", "青岛"],
    "西南": ["成都", "重庆", "昆明"],
    "华中": ["武汉", "长沙", "郑州"],
}
STORE_TYPES = ["直营", "加盟"]
PRODUCT_TYPES = ["数码", "家电", "服饰", "食品"]
CATEGORIES = {
    "数码": ["手机", "笔记本", "平板", "耳机"],
    "家电": ["冰箱", "空调", "洗衣机", "电视"],
    "服饰": ["男装", "女装", "运动", "鞋靴"],
    "食品": ["零食", "生鲜", "饮料"],
}
CHANNELS = ["wechat", "alipay", "card"]
USER_LEVELS = ["normal", "silver", "gold", "platinum"]


def d(dt: date) -> str:
    """date → 'YYYYMMDD'（分区 dt 格式）"""
    return dt.strftime("%Y%m%d")


def run(out: Path, seed: int = 42, days: int = 90) -> Path:
    rng = random.Random(seed)
    today = date.today()
    first = today - timedelta(days=days - 1)

    conn = sqlite3.connect(out)
    # 幂等重建：先清空全部表（schema.sql 是 CREATE IF NOT EXISTS，
    # 旧库存在时直接 INSERT 会主键冲突——重跑/发布必须可重复生成）
    _TABLES = ["dwd_ord_order_di", "dwd_ord_pay_di", "dwd_ord_consume_di",
               "dwd_ord_refund_di", "dws_ord_order_1d", "dws_ord_pay_1d",
               "dws_ord_consume_1d", "dws_ord_refund_1d", "ads_ord_gmv_1d",
               "ads_ord_gmv_1m", "ads_ord_user_1d", "dim_product", "dim_store",
               "dim_region", "dim_user"]
    for t in _TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.executescript(Path(BASE_DIR / "schema.sql").read_text())

    # ── 1. DIM 维度表 ─────────────────────────────────────────
    # dim_region: 每个城市一行（region_id 全局唯一）
    dim_region = []
    region_id_by_city = {}
    rid_counter = 1
    for name in REGIONS:
        for city in CITIES_BY_REGION[name]:
            rid = f"R{rid_counter:02d}"
            rid_counter += 1
            dim_region.append((rid, name, name + "省", city))
            region_id_by_city[city] = rid
    conn.executemany("INSERT INTO dim_region VALUES (?,?,?,?)", dim_region)

    # dim_product: 4 类型 × 每类 4 品类 × 每品类 2~4 个 SKU
    products, prod_rows = [], []
    for pt in PRODUCT_TYPES:
        for cat in CATEGORIES[pt]:
            for k in range(rng.randint(2, 4)):
                pid = f"P{len(products) + 1:04d}"
                products.append((pid, pt, cat))
                prod_rows.append((pid, f"{cat}_{pt}_{k+1}", pt, cat))
    conn.executemany("INSERT INTO dim_product VALUES (?,?,?,?)", prod_rows)

    # dim_store: 每城市 2~3 家店（直营/加盟混合）
    store_rows = []
    for name in REGIONS:
        for city in CITIES_BY_REGION[name]:
            rid = region_id_by_city[city]
            for k in range(rng.randint(2, 3)):
                sid = f"S{len(store_rows) + 1:04d}"
                store_rows.append((sid, f"{city}店{k+1}", rng.choice(STORE_TYPES), city, rid))
    conn.executemany("INSERT INTO dim_store VALUES (?,?,?,?,?)", store_rows)

    # dim_user: 500 用户
    user_rows = []
    for i in range(1, 501):
        uid = f"U{i:06d}"
        reg = first + timedelta(days=rng.randrange(days))
        user_rows.append((uid,
                          "active" if rng.random() < 0.85 else "inactive",
                          rng.choices(USER_LEVELS, weights=[60, 25, 10, 5])[0],
                          d(reg)))
    conn.executemany("INSERT INTO dim_user VALUES (?,?,?,?)", user_rows)

    # ── 2. DWD 明细事实表（近 90 天）───────────────────────────
    order_rows, pay_rows, consume_rows, refund_rows = [], [], [], []
    order_id = pay_id = consume_id = refund_id = 0

    # 预构建索引，避免循环内 O(n) 扫描
    store_ids = [s[0] for s in store_rows]
    store_region = {s[0]: s[4] for s in store_rows}

    for i in range(days):
        day = first + timedelta(days=i)
        dt = d(day)
        # 周末单量多 30%
        n_orders = rng.randint(40, 70) * (13 if day.weekday() >= 5 else 10) // 10
        for _ in range(n_orders):
            uid = f"U{rng.randint(1, 500):06d}"
            pid, pt, cat = rng.choice(products)
            sid = rng.choice(store_ids)
            rid = store_region[sid]                       # O(1) 查门店所属区域
            amt = round(rng.uniform(20, 3000), 2)
            valid = 1 if rng.random() < 0.9 else 0

            order_id += 1
            oid = f"O{order_id:09d}"
            order_rows.append((oid, uid, pid, sid, rid, day.isoformat(), amt,
                               "completed" if valid else "cancelled", valid, dt))
            if valid and rng.random() < 0.92:            # 有效单 92% 支付
                pay_id += 1
                pid2 = f"P{pay_id:09d}"
                pay_amt = round(amt * rng.uniform(0.95, 1.0), 2)
                pay_rows.append((pid2, oid, uid, pid, sid, rid, rng.choice(CHANNELS),
                                 pay_amt, day.isoformat(), 1, dt))
                # 已支付订单 55% 发生到店消费/核销（金额 ≤ 支付额）
                if rng.random() < 0.55:
                    consume_id += 1
                    consume_rows.append((f"C{consume_id:09d}", uid, pid, sid, rid,
                                         round(pay_amt * rng.uniform(0.3, 0.8), 2),
                                         day.isoformat(), 1, dt))
                # 已支付订单 5% 退款
                if rng.random() < 0.05:
                    refund_id += 1
                    refund_rows.append((f"RF{refund_id:09d}", oid, pid2, uid, rid, sid,
                                        round(pay_amt * rng.uniform(0.1, 1.0), 2),
                                        day.isoformat(), 1, dt))

    conn.executemany("INSERT INTO dwd_ord_order_di VALUES (?,?,?,?,?,?,?,?,?,?)", order_rows)
    conn.executemany("INSERT INTO dwd_ord_pay_di VALUES (?,?,?,?,?,?,?,?,?,?,?)", pay_rows)
    conn.executemany("INSERT INTO dwd_ord_consume_di VALUES (?,?,?,?,?,?,?,?,?)", consume_rows)
    conn.executemany("INSERT INTO dwd_ord_refund_di VALUES (?,?,?,?,?,?,?,?,?,?)", refund_rows)

    # ── 3. DWS / ADS：从 DWD 聚合（模拟真实 ETL，保证口径一致）──
    conn.executescript("""
    INSERT INTO dws_ord_order_1d
      SELECT dt, region_id, store_id,
             COUNT(*)                       AS order_cnt,
             SUM(is_valid)                  AS valid_order_cnt,
             SUM(CASE WHEN is_valid=1 THEN order_amt ELSE 0 END) AS order_amt,
             COUNT(DISTINCT CASE WHEN is_valid=1 THEN user_id END) AS order_user_cnt
      FROM dwd_ord_order_di GROUP BY dt, region_id, store_id;

    INSERT INTO dws_ord_pay_1d
      SELECT dt, region_id, store_id,
             COUNT(*)              AS pay_cnt,
             SUM(pay_amt)          AS pay_amt,
             COUNT(DISTINCT user_id) AS pay_user_cnt
      FROM dwd_ord_pay_di WHERE is_valid=1 GROUP BY dt, region_id, store_id;

    INSERT INTO dws_ord_consume_1d
      SELECT dt, region_id, store_id,
             COUNT(*) AS consume_cnt, SUM(consume_amt) AS consume_amt,
             COUNT(DISTINCT user_id) AS consume_user_cnt
      FROM dwd_ord_consume_di WHERE is_valid=1 GROUP BY dt, region_id, store_id;

    INSERT INTO dws_ord_refund_1d
      SELECT dt, region_id, store_id,
             COUNT(*) AS refund_cnt, SUM(refund_amt) AS refund_amt
      FROM dwd_ord_refund_di WHERE is_valid=1 GROUP BY dt, region_id, store_id;

    INSERT INTO ads_ord_gmv_1d
      SELECT dt, SUM(pay_amt) AS gmv_amt, COUNT(*) AS order_cnt,
             COUNT(*) AS pay_cnt, 0 AS refund_amt
      FROM dwd_ord_pay_di WHERE is_valid=1 GROUP BY dt;

    INSERT INTO ads_ord_gmv_1m
      SELECT substr(dt,1,4) || '-' || substr(dt,5,2) AS month_dt,
             SUM(pay_amt), COUNT(*)
      FROM dwd_ord_pay_di WHERE is_valid=1
      GROUP BY substr(dt,1,4) || '-' || substr(dt,5,2);

    INSERT INTO ads_ord_user_1d
      SELECT dt, 0 AS active_user_cnt,
             COUNT(DISTINCT user_id) AS order_user_cnt, 0 AS new_user_cnt
      FROM dwd_ord_pay_di WHERE is_valid=1 GROUP BY dt;
    """)
    conn.commit()

    # 统计信息
    n = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
         for t in ["dwd_ord_order_di", "dwd_ord_pay_di", "dwd_ord_consume_di",
                   "dwd_ord_refund_di", "dws_ord_pay_1d", "ads_ord_gmv_1d",
                   "ads_ord_gmv_1m", "dim_product", "dim_store", "dim_region", "dim_user"]}
    conn.close()
    print(f"✅ 样例库已生成: {out}（今天={today.isoformat()}，t-1={d(today - timedelta(days=1))}）")
    for t, c in n.items():
        print(f"   {t}: {c} 行")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(BASE_DIR / "sample.db"))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--days", type=int, default=90)
    a = ap.parse_args()
    run(Path(a.out), seed=a.seed, days=a.days)


if __name__ == "__main__":
    main()
