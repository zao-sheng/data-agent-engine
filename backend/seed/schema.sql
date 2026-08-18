-- ============================================================
-- data-agent-engine · order 主题数仓样例 DDL
-- 分层：DWD（明细） / DWS（日汇总） / ADS（应用汇总） / DIM（维度）
-- 约定：
--   * 所有事实表（DWD/DWS/ADS）时间分区列统一为 dt（TEXT, 'YYYYMMDD'）
--   * 部分字段在不同层重复存在（region_id/store_id/pay_amt/order_amt…），
--     模拟真实数仓的字段冗余，供表选择逻辑演示"同指标多表可取"
--   * 明细表带 is_valid（1=有效，0=无效/取消），供 required_filter 演示
-- ============================================================

-- ── DWD：下单域 ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dwd_ord_order_di (
  order_id    TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  product_id  TEXT NOT NULL,
  store_id    TEXT NOT NULL,
  region_id   TEXT NOT NULL,
  order_date  TEXT NOT NULL,          -- 业务日期 'YYYY-MM-DD'（与 dt 同日）
  order_amt   REAL NOT NULL,
  order_status TEXT NOT NULL DEFAULT 'completed',   -- completed / cancelled
  is_valid    INTEGER NOT NULL DEFAULT 1,           -- 1=有效 0=取消
  dt          TEXT NOT NULL                          -- 分区 'YYYYMMDD'
);
CREATE INDEX IF NOT EXISTS idx_order_dt ON dwd_ord_order_di(dt);
CREATE INDEX IF NOT EXISTS idx_order_store ON dwd_ord_order_di(store_id);

-- ── DWD：支付域 ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dwd_ord_pay_di (
  pay_id     TEXT PRIMARY KEY,
  order_id   TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  product_id TEXT NOT NULL,
  store_id   TEXT NOT NULL,
  region_id  TEXT NOT NULL,
  channel    TEXT NOT NULL,           -- 支付渠道: wechat/alipay/card
  pay_amt    REAL NOT NULL,
  pay_date   TEXT NOT NULL,
  is_valid   INTEGER NOT NULL DEFAULT 1,
  dt         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pay_dt ON dwd_ord_pay_di(dt);
CREATE INDEX IF NOT EXISTS idx_pay_order ON dwd_ord_pay_di(order_id);

-- ── DWD：消费域（核销/到店消费）────────────────────────────
CREATE TABLE IF NOT EXISTS dwd_ord_consume_di (
  consume_id   TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL,
  product_id   TEXT NOT NULL,
  store_id     TEXT NOT NULL,
  region_id    TEXT NOT NULL,
  consume_amt  REAL NOT NULL,
  consume_date TEXT NOT NULL,
  is_valid     INTEGER NOT NULL DEFAULT 1,
  dt           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consume_dt ON dwd_ord_consume_di(dt);

-- ── DWD：退款域 ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dwd_ord_refund_di (
  refund_id   TEXT PRIMARY KEY,
  order_id    TEXT NOT NULL,
  pay_id      TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  region_id   TEXT NOT NULL,          -- 冗余：原支付所属区域（模拟真实数仓冗余）
  store_id    TEXT NOT NULL,          -- 冗余：原支付所属门店
  refund_amt  REAL NOT NULL,
  refund_date TEXT NOT NULL,
  is_valid    INTEGER NOT NULL DEFAULT 1,
  dt          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refund_dt ON dwd_ord_refund_di(dt);

-- ── DWS：下单日汇总 ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dws_ord_order_1d (
  dt             TEXT NOT NULL,       -- 分区 'YYYYMMDD'
  region_id      TEXT NOT NULL,
  store_id       TEXT NOT NULL,
  order_cnt      INTEGER NOT NULL,    -- 全部订单数
  valid_order_cnt INTEGER NOT NULL,   -- 有效订单数
  order_amt      REAL NOT NULL,       -- 有效订单金额
  order_user_cnt INTEGER NOT NULL     -- 下单用户数（去重）
);
CREATE INDEX IF NOT EXISTS idx_dws_order_dt ON dws_ord_order_1d(dt);

-- ── DWS：支付日汇总 ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dws_ord_pay_1d (
  dt           TEXT NOT NULL,
  region_id    TEXT NOT NULL,
  store_id     TEXT NOT NULL,
  pay_cnt      INTEGER NOT NULL,
  pay_amt      REAL NOT NULL,         -- 有效支付金额（=GMV）
  pay_user_cnt INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dws_pay_dt ON dws_ord_pay_1d(dt);

-- ── DWS：消费日汇总 ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dws_ord_consume_1d (
  dt              TEXT NOT NULL,
  region_id       TEXT NOT NULL,
  store_id        TEXT NOT NULL,
  consume_cnt     INTEGER NOT NULL,
  consume_amt     REAL NOT NULL,
  consume_user_cnt INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dws_consume_dt ON dws_ord_consume_1d(dt);

-- ── DWS：退款日汇总 ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dws_ord_refund_1d (
  dt          TEXT NOT NULL,
  region_id   TEXT NOT NULL,
  store_id    TEXT NOT NULL,
  refund_cnt  INTEGER NOT NULL,
  refund_amt  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dws_refund_dt ON dws_ord_refund_1d(dt);

-- ── ADS：应用层 ────────────────────────────────────────────
-- 日 GMV（整库口径，无维度细分）
CREATE TABLE IF NOT EXISTS ads_ord_gmv_1d (
  dt         TEXT NOT NULL,           -- 分区 'YYYYMMDD'
  gmv_amt    REAL NOT NULL,           -- 日 GMV = SUM(有效支付金额)
  order_cnt  INTEGER NOT NULL,
  pay_cnt    INTEGER NOT NULL,
  refund_amt REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ads_gmv_dt ON ads_ord_gmv_1d(dt);

-- 月 GMV
CREATE TABLE IF NOT EXISTS ads_ord_gmv_1m (
  month_dt   TEXT NOT NULL,           -- 'YYYY-MM'
  gmv_amt    REAL NOT NULL,
  order_cnt  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ads_gmv_m ON ads_ord_gmv_1m(month_dt);

-- 日用户活跃/下单
CREATE TABLE IF NOT EXISTS ads_ord_user_1d (
  dt             TEXT NOT NULL,
  active_user_cnt INTEGER NOT NULL,   -- 活跃用户数
  order_user_cnt  INTEGER NOT NULL,   -- 下单用户数
  new_user_cnt    INTEGER NOT NULL    -- 新注册用户数
);
CREATE INDEX IF NOT EXISTS idx_ads_user_dt ON ads_ord_user_1d(dt);

-- ── DIM：维度表 ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dim_product (
  product_id   TEXT PRIMARY KEY,
  product_name TEXT NOT NULL,
  product_type TEXT NOT NULL,         -- 数码 / 家电 / 服饰 / 食品
  category     TEXT NOT NULL          -- 大类（如 手机/笔记本…）
);
CREATE TABLE IF NOT EXISTS dim_store (
  store_id   TEXT PRIMARY KEY,
  store_name TEXT NOT NULL,
  store_type TEXT NOT NULL,           -- 直营 / 加盟
  city       TEXT NOT NULL,
  region_id  TEXT NOT NULL            -- 冗余：门店所属区域
);
CREATE TABLE IF NOT EXISTS dim_region (
  region_id   TEXT PRIMARY KEY,
  region_name TEXT NOT NULL,          -- 华东 / 华南 / 华北 / 西南 / 华中
  province    TEXT NOT NULL,
  city        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dim_user (
  user_id       TEXT PRIMARY KEY,
  user_status   TEXT NOT NULL,        -- active / inactive
  user_level    TEXT NOT NULL,        -- normal / silver / gold / platinum
  register_date TEXT NOT NULL
);
