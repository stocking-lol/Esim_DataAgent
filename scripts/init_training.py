"""
eSIM NL2SQL 领域知识初始化脚本
-----------------------------
将 DDL、业务文档、SQL 示例注入 ChromaDB，
增强 Vanna Agent 对 eSIM 业务领域的理解。

用法:
    # 独立运行（需要先启动 FastAPI 确保 ChromaDB 可用）
    python scripts/init_training.py

    # 或在 FastAPI 启动时自动执行（由 app/main.py 控制）
"""

import asyncio
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))

from app.core.chroma_store import chroma_store
from app.config.settings import settings


# ============================================================
# DDL 训练数据（7 张表的完整 DDL）
# ============================================================

DDL_DATA = [
    # 运营商表
    (
        """CREATE TABLE operators (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(100)    NOT NULL COMMENT '运营商名称',
    type        ENUM('MNO', 'MVNO') NOT NULL COMMENT '运营商类型：MNO移动网络运营商/MVNO虚拟运营商',
    mcc_mnc     VARCHAR(6)      NOT NULL COMMENT 'MCC+MNC码（移动国家码+移动网络码）',
    country     VARCHAR(100)    NOT NULL COMMENT '所属国家/地区',
    status      ENUM('active','inactive') NOT NULL DEFAULT 'active' COMMENT '运营状态',
    contact_info VARCHAR(200)   DEFAULT NULL COMMENT '联系方式',
    created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_mcc_mnc (mcc_mnc),
    INDEX idx_type (type),
    INDEX idx_country (country),
    INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='运营商信息表'""",
        "operators",
    ),
    # 用户表
    (
        """CREATE TABLE users (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    phone_number VARCHAR(20)    DEFAULT NULL COMMENT '手机号码',
    email       VARCHAR(255)    DEFAULT NULL COMMENT '邮箱地址',
    iccid       VARCHAR(22)     NOT NULL COMMENT 'eSIM芯片唯一标识符（19-20位，8986开头）',
    imsi        VARCHAR(15)     DEFAULT NULL COMMENT 'IMSI国际移动用户身份码（MCC+MNC+MSIN）',
    mvno_id     INT             NOT NULL COMMENT '所属虚拟运营商ID',
    status      ENUM('active','inactive','suspended') NOT NULL DEFAULT 'active' COMMENT '用户状态',
    region      VARCHAR(100)    DEFAULT NULL COMMENT '所属地区（国家/城市）',
    created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间',
    updated_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_iccid (iccid),
    UNIQUE KEY uk_imsi (imsi),
    INDEX idx_mvno_id (mvno_id),
    INDEX idx_status (status),
    INDEX idx_region (region),
    INDEX idx_created_at (created_at),
    INDEX idx_status_created (status, created_at),
    CONSTRAINT fk_users_mvno FOREIGN KEY (mvno_id) REFERENCES operators(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='eSIM用户信息表'""",
        "users",
    ),
    # 套餐表
    (
        """CREATE TABLE plans (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(200)    NOT NULL COMMENT '套餐名称',
    data_volume_mb  INT             NOT NULL DEFAULT 0 COMMENT '数据流量（MB）',
    voice_minutes   INT             NOT NULL DEFAULT 0 COMMENT '语音分钟数',
    sms_count       INT             NOT NULL DEFAULT 0 COMMENT '短信条数',
    price           DECIMAL(10,2)   NOT NULL DEFAULT 0.00 COMMENT '套餐价格',
    currency        VARCHAR(10)     NOT NULL DEFAULT 'CNY' COMMENT '币种',
    validity_days   INT             NOT NULL DEFAULT 30 COMMENT '有效期（天）',
    type            ENUM('local','roaming','global') NOT NULL COMMENT '套餐类型：本地/漫游/全球',
    mvno_id         INT             NOT NULL COMMENT '所属运营商ID',
    status          ENUM('active','inactive','discontinued') NOT NULL DEFAULT 'active' COMMENT '套餐状态',
    description     TEXT            DEFAULT NULL COMMENT '套餐描述',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    INDEX idx_type (type),
    INDEX idx_mvno_id (mvno_id),
    INDEX idx_status (status),
    INDEX idx_price (price),
    CONSTRAINT fk_plans_mvno FOREIGN KEY (mvno_id) REFERENCES operators(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='套餐信息表'""",
        "plans",
    ),
    # 订单表
    (
        """CREATE TABLE orders (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    user_id         INT             NOT NULL COMMENT '用户ID',
    plan_id         INT             NOT NULL COMMENT '套餐ID',
    order_no        VARCHAR(50)     NOT NULL COMMENT '订单号',
    status          ENUM('pending','paid','activated','cancelled','refunded') NOT NULL DEFAULT 'pending' COMMENT '订单状态',
    amount          DECIMAL(10,2)   NOT NULL DEFAULT 0.00 COMMENT '订单金额',
    currency        VARCHAR(10)     NOT NULL DEFAULT 'CNY' COMMENT '币种',
    payment_method  VARCHAR(50)     DEFAULT NULL COMMENT '支付方式',
    mvno_id         INT             NOT NULL COMMENT '所属运营商ID',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '下单时间',
    activated_at    DATETIME        DEFAULT NULL COMMENT '激活时间',
    cancelled_at    DATETIME        DEFAULT NULL COMMENT '取消时间',
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_order_no (order_no),
    INDEX idx_user_id (user_id),
    INDEX idx_plan_id (plan_id),
    INDEX idx_status (status),
    INDEX idx_created_at (created_at),
    CONSTRAINT fk_orders_user FOREIGN KEY (user_id) REFERENCES users(id),
    CONSTRAINT fk_orders_plan FOREIGN KEY (plan_id) REFERENCES plans(id),
    CONSTRAINT fk_orders_mvno FOREIGN KEY (mvno_id) REFERENCES operators(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='订单信息表'""",
        "orders",
    ),
    # Profile 表
    (
        """CREATE TABLE esim_profiles (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    user_id         INT             NOT NULL COMMENT '用户ID',
    iccid           VARCHAR(22)     NOT NULL COMMENT 'eSIM芯片标识符',
    imsi            VARCHAR(15)     DEFAULT NULL COMMENT 'IMSI用户身份模块标识',
    profile_status  ENUM('downloaded','installed','active','enabled','disabled','deleted') NOT NULL DEFAULT 'downloaded' COMMENT 'Profile状态',
    activation_code VARCHAR(100)    DEFAULT NULL COMMENT '激活码',
    mno_id          INT             NOT NULL COMMENT '归属MNO运营商ID',
    mvno_id         INT             NOT NULL COMMENT '所属MVNO运营商ID',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    activated_at    DATETIME        DEFAULT NULL COMMENT '激活时间',
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    INDEX idx_user_id (user_id),
    INDEX idx_iccid (iccid),
    INDEX idx_profile_status (profile_status),
    INDEX idx_mno_id (mno_id),
    INDEX idx_mvno_id (mvno_id),
    INDEX idx_created_at (created_at),
    CONSTRAINT fk_profiles_user FOREIGN KEY (user_id) REFERENCES users(id),
    CONSTRAINT fk_profiles_mno FOREIGN KEY (mno_id) REFERENCES operators(id),
    CONSTRAINT fk_profiles_mvno FOREIGN KEY (mvno_id) REFERENCES operators(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='eSIM Profile管理表'""",
        "esim_profiles",
    ),
    # 流量使用表
    (
        """CREATE TABLE data_usage (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    user_id         INT             NOT NULL COMMENT '用户ID',
    iccid           VARCHAR(22)     NOT NULL COMMENT 'eSIM芯片标识符',
    usage_mb        DECIMAL(10,2)   NOT NULL DEFAULT 0.00 COMMENT '使用流量（MB）',
    roaming_flag    TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '是否漫游：0=否 1=是',
    country_code    VARCHAR(10)     DEFAULT NULL COMMENT '使用地国家码（如CN/US/JP）',
    usage_date      DATE            NOT NULL COMMENT '使用日期',
    recorded_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录时间',
    INDEX idx_user_id (user_id),
    INDEX idx_iccid (iccid),
    INDEX idx_usage_date (usage_date),
    INDEX idx_roaming_flag (roaming_flag),
    INDEX idx_country_code (country_code),
    INDEX idx_user_date (user_id, usage_date),
    INDEX idx_date_roaming (usage_date, roaming_flag),
    CONSTRAINT fk_usage_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='流量使用记录表'""",
        "data_usage",
    ),
    # 漫游包表
    (
        """CREATE TABLE roaming_packages (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(200)    NOT NULL COMMENT '漫游包名称',
    countries       VARCHAR(500)    NOT NULL COMMENT '适用国家/地区（逗号分隔）',
    data_volume_mb  INT             NOT NULL DEFAULT 0 COMMENT '数据流量（MB）',
    duration_days   INT             NOT NULL DEFAULT 1 COMMENT '有效时长（天）',
    price           DECIMAL(10,2)   NOT NULL DEFAULT 0.00 COMMENT '价格',
    currency        VARCHAR(10)     NOT NULL DEFAULT 'CNY' COMMENT '币种',
    operator_id     INT             NOT NULL COMMENT '运营商ID',
    status          ENUM('active','inactive') NOT NULL DEFAULT 'active' COMMENT '状态',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    INDEX idx_operator_id (operator_id),
    INDEX idx_status (status),
    INDEX idx_price (price),
    CONSTRAINT fk_roaming_operator FOREIGN KEY (operator_id) REFERENCES operators(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='漫游包信息表'""",
        "roaming_packages",
    ),
]


# ============================================================
# 业务文档训练数据
# ============================================================

DOCUMENTATION_DATA = [
    # 核心概念
    (
        "eSIM Profile 是嵌入在设备芯片中的数字 SIM 卡配置文件，包含 IMSI 和鉴权密钥，"
        "通过激活码下载到 eUICC 芯片。Profile 状态包括：downloaded（已下载）、"
        "installed（已安装）、active（已激活）、enabled（已启用）、disabled（已禁用）、"
        "deleted（已删除）。",
        "eSIM Profile",
    ),
    (
        "ICCID 是 eSIM 芯片的唯一标识符，共 19-20 位数字，以 8986 开头。"
        "在 users 表和 esim_profiles 表中均有 iccid 字段，可通过 iccid 关联用户和 Profile。",
        "ICCID",
    ),
    (
        "IMSI 是国际移动用户身份码，用于网络鉴权，由 MCC（国家码）+ MNC（运营商码）"
        "+ MSIN（用户识别码）组成。IMSI 存储在 users 表和 esim_profiles 表中，"
        "长度不超过 15 位。",
        "IMSI",
    ),
    (
        "MNO（Mobile Network Operator）是移动网络运营商，如中国移动、中国联通等，"
        "拥有自己的无线网络基础设施。MVNO（Mobile Virtual Network Operator）是虚拟运营商，"
        "租用 MNO 网络向用户提供服务。在 operators 表中 type 字段区分 MNO 和 MVNO。",
        "MNO/MVNO",
    ),

    # 业务指标
    (
        "Profile 激活转化率 = 成功激活 Profile 数（profile_status='active'）"
        " / 下载 Profile 数（profile_status='downloaded'） * 100%。"
        "该指标衡量 eSIM Profile 从下载到实际使用的转化效率。",
        "Profile激活转化率",
    ),
    (
        "漫游是指用户在非归属运营商网络中使用服务。在 data_usage 表中，"
        "roaming_flag=1 表示漫游流量。漫游包（roaming_packages 表）是针对特定国家或地区的流量套餐，"
        "漫游流量占比 = 漫游流量总和 / 总流量总和 * 100%。",
        "漫游",
    ),
    (
        "ARPU 值（Average Revenue Per User）= 总收入 / 活跃用户数。"
        "总收入可从 orders 表的 amount 字段求和（仅统计 status IN ('paid','activated') 的订单）获得，"
        "活跃用户定义为最近 30 天内有流量使用记录的用户。",
        "ARPU",
    ),
    (
        "活跃用户定义：最近 30 天内有流量使用记录的用户。"
        "查询方法：SELECT COUNT(DISTINCT user_id) FROM data_usage "
        "WHERE usage_date >= DATE_SUB(CURRENT_DATE, INTERVAL 30 DAY)。",
        "活跃用户",
    ),
    (
        "用户离网率 = 本期流失用户数（status 变为 'inactive' 或 'suspended' 的用户）"
        " / 期初活跃用户数 * 100%。"
        "可通过 users 表的 status 字段变化来计算，通常按月统计。",
        "用户离网率",
    ),

    # 表关系说明
    (
        "users 表和 esim_profiles 表通过 user_id 关联（一对多：一个用户可以有多个 Profile）。"
        "orders 表关联 users（user_id）和 plans（plan_id）。"
        "data_usage 表通过 user_id 关联 users，通过 iccid 关联具体的 eSIM 芯片。"
        "所有业务表（users/plans/orders/esim_profiles/roaming_packages）通过 mvno_id 关联 operators 表。",
        "表关系",
    ),
    (
        "plans 表的 type 字段取值：'local'（本地套餐）、'roaming'（漫游套餐）、'global'（全球套餐）。"
        "orders 表的 status 字段：'pending'（待支付）、'paid'（已支付）、'activated'（已激活）、"
        "'cancelled'（已取消）、'refunded'（已退款）。"
        "data_usage 表的 roaming_flag：0 表示本地流量，1 表示漫游流量。",
        "字段枚举值",
    ),

    # 分析场景
    (
        "套餐销量排名：通过 JOIN orders 和 plans 表，按 plan_id 分组统计订单数量，"
        "关联 plans 表获取套餐名称。"
        "SQL: SELECT p.name, COUNT(o.id) as order_count FROM plans p "
        "LEFT JOIN orders o ON p.id = o.plan_id "
        "GROUP BY p.id, p.name ORDER BY order_count DESC。",
        "套餐销量分析",
    ),
    (
        "月度收入趋势：按订单创建日期的月份分组，统计该月所有已支付/已激活订单的总金额。"
        "SQL: SELECT DATE_FORMAT(created_at, '%Y-%m') as month, "
        "SUM(amount) as revenue FROM orders "
        "WHERE status IN ('paid','activated') GROUP BY month ORDER BY month。",
        "收入分析",
    ),
    (
        "用户流量使用 TOP10 排名：按用户分组统计总流量使用量，取前 10。"
        "SQL: SELECT u.id, u.phone_number, SUM(d.usage_mb) as total_usage "
        "FROM users u JOIN data_usage d ON u.id = d.user_id "
        "GROUP BY u.id, u.phone_number ORDER BY total_usage DESC LIMIT 10。",
        "流量TOP10",
    ),
]


# ============================================================
# SQL 示例训练数据
# ============================================================

SQL_EXAMPLES = [
    # ---- 单表查询 ----
    {
        "question": "本月新增多少eSIM用户",
        "sql": "SELECT COUNT(*) AS new_users FROM users WHERE created_at >= DATE_FORMAT(CURRENT_DATE, '%Y-%m-01')",
    },
    {
        "question": "各状态用户数量统计",
        "sql": "SELECT status, COUNT(*) AS user_count FROM users GROUP BY status ORDER BY user_count DESC",
    },
    {
        "question": "中国地区的活跃用户列表",
        "sql": "SELECT id, phone_number, email, created_at FROM users WHERE region = 'China' AND status = 'active'",
    },
    {
        "question": "所有在售套餐列表",
        "sql": "SELECT id, name, data_volume_mb, voice_minutes, price, type FROM plans WHERE status = 'active' ORDER BY price",
    },

    # ---- 多表 JOIN ----
    {
        "question": "各套餐的订购人数和总收入",
        "sql": "SELECT p.name AS plan_name, COUNT(o.id) AS order_count, SUM(o.amount) AS total_revenue FROM plans p LEFT JOIN orders o ON p.id = o.plan_id AND o.status IN ('paid','activated') GROUP BY p.id, p.name ORDER BY total_revenue DESC",
    },
    {
        "question": "用户最近一次订单信息",
        "sql": "SELECT u.id, u.phone_number, o.order_no, o.status, o.amount, o.created_at FROM users u JOIN orders o ON u.id = o.user_id WHERE o.created_at = (SELECT MAX(created_at) FROM orders WHERE user_id = u.id)",
    },
    {
        "question": "每个用户的流量使用总量",
        "sql": "SELECT u.id, u.phone_number, COALESCE(SUM(d.usage_mb), 0) AS total_usage_mb FROM users u LEFT JOIN data_usage d ON u.id = d.user_id GROUP BY u.id, u.phone_number ORDER BY total_usage_mb DESC",
    },

    # ---- 聚合统计 ----
    {
        "question": "各运营商ARPU值",
        "sql": "SELECT op.name AS operator_name, ROUND(SUM(o.amount) / GREATEST(COUNT(DISTINCT o.user_id), 1), 2) AS arpu FROM operators op JOIN orders o ON op.id = o.mvno_id WHERE o.status IN ('paid','activated') GROUP BY op.id, op.name ORDER BY arpu DESC",
    },
    {
        "question": "月度收入趋势",
        "sql": "SELECT DATE_FORMAT(created_at, '%Y-%m') AS month, SUM(amount) AS revenue, COUNT(*) AS order_count FROM orders WHERE status IN ('paid','activated') GROUP BY month ORDER BY month",
    },
    {
        "question": "套餐销量排名",
        "sql": "SELECT p.name AS plan_name, p.type, p.price, COUNT(o.id) AS order_count FROM plans p LEFT JOIN orders o ON p.id = o.plan_id AND o.status IN ('paid','activated') GROUP BY p.id, p.name, p.type, p.price ORDER BY order_count DESC",
    },

    # ---- Profile 分析 ----
    {
        "question": "Profile激活转化率",
        "sql": "SELECT ROUND(SUM(CASE WHEN profile_status = 'active' THEN 1 ELSE 0 END) * 100.0 / GREATEST(COUNT(*), 1), 2) AS activation_rate FROM esim_profiles WHERE profile_status IN ('downloaded','active')",
    },
    {
        "question": "各状态Profile分布",
        "sql": "SELECT profile_status, COUNT(*) AS profile_count FROM esim_profiles GROUP BY profile_status ORDER BY profile_count DESC",
    },
    {
        "question": "各运营商上月的Profile激活转化率",
        "sql": "SELECT op.name AS operator_name, COUNT(DISTINCT CASE WHEN ep.profile_status = 'active' AND ep.activated_at >= DATE_FORMAT(DATE_SUB(CURRENT_DATE, INTERVAL 1 MONTH), '%Y-%m-01') THEN ep.id END) AS activated_count, COUNT(DISTINCT ep.id) AS total_count, ROUND(COUNT(DISTINCT CASE WHEN ep.profile_status = 'active' AND ep.activated_at >= DATE_FORMAT(DATE_SUB(CURRENT_DATE, INTERVAL 1 MONTH), '%Y-%m-01') THEN ep.id END) * 100.0 / GREATEST(COUNT(DISTINCT ep.id), 1), 2) AS conversion_rate FROM operators op LEFT JOIN esim_profiles ep ON op.id = ep.mvno_id GROUP BY op.id, op.name",
    },

    # ---- 流量分析 ----
    {
        "question": "用户平均流量使用量",
        "sql": "SELECT ROUND(AVG(user_total), 2) AS avg_usage_mb FROM (SELECT user_id, SUM(usage_mb) AS user_total FROM data_usage GROUP BY user_id) AS t",
    },
    {
        "question": "漫游流量占总流量的比例",
        "sql": "SELECT ROUND(SUM(CASE WHEN roaming_flag = 1 THEN usage_mb ELSE 0 END) * 100.0 / GREATEST(SUM(usage_mb), 1), 2) AS roaming_percentage FROM data_usage",
    },
    {
        "question": "流量使用TOP10用户",
        "sql": "SELECT u.id, u.phone_number, SUM(d.usage_mb) AS total_usage_mb FROM users u JOIN data_usage d ON u.id = d.user_id GROUP BY u.id, u.phone_number ORDER BY total_usage_mb DESC LIMIT 10",
    },

    # ---- 运营商分析 ----
    {
        "question": "各运营商的活跃用户数",
        "sql": "SELECT op.name AS operator_name, COUNT(DISTINCT u.id) AS active_users FROM operators op LEFT JOIN users u ON op.id = u.mvno_id AND u.status = 'active' LEFT JOIN data_usage d ON u.id = d.user_id AND d.usage_date >= DATE_SUB(CURRENT_DATE, INTERVAL 30 DAY) WHERE op.type = 'MVNO' GROUP BY op.id, op.name ORDER BY active_users DESC",
    },
    {
        "question": "各运营商本月收入排名",
        "sql": "SELECT op.name AS operator_name, SUM(o.amount) AS revenue, COUNT(o.id) AS order_count FROM operators op JOIN orders o ON op.id = o.mvno_id WHERE o.status IN ('paid','activated') AND o.created_at >= DATE_FORMAT(CURRENT_DATE, '%Y-%m-01') GROUP BY op.id, op.name ORDER BY revenue DESC",
    },

    # ---- 漫游分析 ----
    {
        "question": "各国家/地区的漫游流量排名",
        "sql": "SELECT country_code, SUM(usage_mb) AS roaming_usage_mb, COUNT(*) AS record_count FROM data_usage WHERE roaming_flag = 1 GROUP BY country_code ORDER BY roaming_usage_mb DESC",
    },
    {
        "question": "漫游包销量统计",
        "sql": "SELECT rp.name, rp.countries, rp.price, COUNT(o.id) AS purchase_count FROM roaming_packages rp LEFT JOIN orders o ON rp.id = o.plan_id AND o.status IN ('paid','activated') WHERE rp.status = 'active' GROUP BY rp.id, rp.name, rp.countries, rp.price ORDER BY purchase_count DESC",
    },
    {
        "question": "各地区漫游订单数",
        "sql": "SELECT u.region, COUNT(o.id) AS roaming_order_count FROM orders o JOIN plans p ON o.plan_id = p.id JOIN users u ON o.user_id = u.id WHERE p.type = 'roaming' AND o.status IN ('paid','activated') GROUP BY u.region ORDER BY roaming_order_count DESC",
    },
    {
        "question": "上周各地区的漫游订单数",
        "sql": "SELECT u.region, COUNT(o.id) AS roaming_order_count FROM orders o JOIN plans p ON o.plan_id = p.id JOIN users u ON o.user_id = u.id WHERE p.type = 'roaming' AND o.status IN ('paid','activated') AND o.created_at >= DATE_SUB(CURRENT_DATE, INTERVAL 7 DAY) GROUP BY u.region ORDER BY roaming_order_count DESC",
    },
]


# ============================================================
# 初始化逻辑
# ============================================================

async def init_all_training_data(force: bool = False) -> dict:
    """初始化所有训练数据到 ChromaDB

    Args:
        force: 是否强制重新初始化（清空已有数据）

    Returns:
        dict: 初始化统计结果
    """
    print("=" * 60)
    print("  eSIM NL2SQL 领域知识初始化")
    print("=" * 60)

    # 1. 初始化 ChromaDB
    print("\n[1/5] 初始化 ChromaDB 存储...")
    await chroma_store.initialize()

    # 检查是否已有数据
    counts = chroma_store.count_by_type()
    total_existing = sum(counts.values())

    if total_existing > 0:
        if force:
            print(f"  已有 {total_existing} 条训练数据，强制重新初始化...")
            chroma_store.clear_all()
        else:
            print(f"  已有 {total_existing} 条训练数据，跳过初始化。")
            print(f"  (使用 --force 参数强制重新初始化)")
            return {
                "status": "skipped",
                "message": f"训练数据已存在（{total_existing} 条），跳过初始化",
                "existing_counts": counts,
            }
    else:
        print("  无已有训练数据，开始初始化...")

    result = {"status": "success", "ddl": 0, "documentation": 0, "sql_examples": 0}

    # 2. 训练 DDL
    print(f"\n[2/5] 训练 DDL ({len(DDL_DATA)} 条)...")
    for ddl, table_name in DDL_DATA:
        chroma_store.add_ddl(ddl, table_name=table_name)
        result["ddl"] += 1
        print(f"  [DDL] {table_name}")
    print(f"  完成：{result['ddl']} 条 DDL 已训练")

    # 3. 训练业务文档
    print(f"\n[3/5] 训练业务文档 ({len(DOCUMENTATION_DATA)} 条)...")
    for doc, topic in DOCUMENTATION_DATA:
        chroma_store.add_documentation(doc, topic=topic)
        result["documentation"] += 1
        print(f"  [DOC] {topic}")
    print(f"  完成：{result['documentation']} 条文档已训练")

    # 4. 训练 SQL 示例
    print(f"\n[4/5] 训练 SQL 示例 ({len(SQL_EXAMPLES)} 条)...")
    for ex in SQL_EXAMPLES:
        chroma_store.add_sql_example(question=ex["question"], sql=ex["sql"])
        result["sql_examples"] += 1
        print(f"  [SQL] {ex['question'][:50]}")
    print(f"  完成：{result['sql_examples']} 条 SQL 示例已训练")

    # 5. 验证
    print("\n[5/5] 验证训练数据...")
    final_counts = chroma_store.count_by_type()
    total = sum(final_counts.values())
    print(f"  DDL:          {final_counts['ddl']}")
    print(f"  Documentation: {final_counts['documentation']}")
    print(f"  SQL Examples:  {final_counts['sql_examples']}")
    print(f"  总计:          {total}")

    result["total"] = total
    result["counts"] = final_counts

    print("\n" + "=" * 60)
    print(f"  ✓ eSIM 领域知识初始化完成！共 {total} 条训练数据")
    print("=" * 60)

    return result


async def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description="初始化 eSIM NL2SQL 训练数据")
    parser.add_argument(
        "--force", action="store_true",
        help="强制重新初始化（清空已有训练数据）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅检查当前状态，不执行初始化",
    )
    args = parser.parse_args()

    if args.dry_run:
        await chroma_store.initialize()
        counts = chroma_store.count_by_type()
        print("当前训练数据状态：")
        for name, count in counts.items():
            print(f"  {name}: {count}")
        print(f"  总计: {sum(counts.values())}")
        return

    result = await init_all_training_data(force=args.force)
    return result


if __name__ == "__main__":
    asyncio.run(main())
