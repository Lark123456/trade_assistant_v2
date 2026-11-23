# -*- coding: utf-8 -*-
"""冒烟自测：实际数据库升级（v0.9.0 新增列）"""
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)


def test_db_migration_with_existing_db():
    """模拟「已有数据库」升级到 v0.9.0，验证新列自动添加"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name

    try:
        # 1) 先打开旧版本（不升级）—— 通过直接初始化来模拟
        import sqlite3
        conn = sqlite3.connect(tmp_path)
        conn.executescript("""
        CREATE TABLE strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT,
            name TEXT NOT NULL,
            kind TEXT DEFAULT 'formula',
            formula TEXT,
            conditions TEXT,
            description TEXT,
            enabled INTEGER DEFAULT 1,
            scan_mode TEXT DEFAULT 'batch',
            created_at TEXT,
            updated_at TEXT,
            tdx_alias TEXT,
            parent_strategy_id INTEGER,
            level INTEGER NOT NULL DEFAULT 1,
            tags TEXT NOT NULL DEFAULT '',
            parent_strategy_ids TEXT NOT NULL DEFAULT '[]'
        );
        INSERT INTO strategies(code, name, kind, formula, scan_mode, enabled)
        VALUES('S0001', '旧策略', 'formula', '白线>黄线', 'batch', 1);
        """)
        conn.commit()
        conn.close()

        # 2) 用 Database 打开 → 应该触发 _migrate 添加 v0.9.0 列
        from database import Database
        db = Database(tmp_path)

        # 3) 验证新增列已存在
        cur = db._conn.cursor()
        cols = {r[1] for r in cur.execute("PRAGMA table_info(strategies)").fetchall()}
        new_cols = {
            "backtest_win_rate", "backtest_profit_loss_ratio", "backtest_total_signals",
            "backtest_avg_ret", "backtest_avg_holding_days",
            "backtest_forward_returns", "backtest_config", "backtest_updated_at",
        }
        missing = new_cols - cols
        assert not missing, f"未自动添加的列: {missing}"

        # 4) 验证 strategy_backtest 表能创建并写入
        db.save_backtest_stats(
            strategy_id=1, win_rate=68.5, profit_loss_ratio=1.85,
            total_signals=200, avg_ret=1.2, avg_holding_days=5.0,
            forward_returns={"1": {"pct_avg": 0.5, "win_rate": 55.0, "count": 200}},
            config={"tp_pct": 0.10, "sl_pct": 0.04, "max_days": 10},
        )
        latest = db.get_latest_backtest_stats(1)
        assert latest is not None
        assert latest["win_rate"] == 68.5

        # 5) 验证 strategies 表的 backtest_win_rate 列已更新
        rows = db.list_strategies()
        assert rows[0]["backtest_win_rate"] == 68.5

        db.close()
        print("✅ test_db_migration_with_existing_db 通过")
        os.unlink(tmp_path)
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def test_real_db_load():
    """直接打开项目现有 trade_assistant.db，验证迁移无报错"""
    db_path = os.path.join(PROJECT_ROOT, "trade_assistant.db")
    if not os.path.exists(db_path):
        print(f"⚠️ 跳过 test_real_db_load：{db_path} 不存在")
        return

    try:
        from database import Database
        db = Database(db_path)
        # 简单查询
        rows = db.list_strategies(enabled_only=False)
        print(f"  → 数据库策略数: {len(rows)}")
        # 检查 v0.9.0 新列是否存在
        cur = db._conn.cursor()
        cols = {r[1] for r in cur.execute("PRAGMA table_info(strategies)").fetchall()}
        new_cols = {
            "backtest_win_rate", "backtest_profit_loss_ratio", "backtest_total_signals",
            "backtest_avg_ret", "backtest_avg_holding_days",
            "backtest_forward_returns", "backtest_config", "backtest_updated_at",
        }
        missing = new_cols - cols
        assert not missing, f"迁移后缺失列: {missing}"
        # 检查 strategy_backtest 表是否存在（懒创建：save_backtest_stats 时才建）
        row = cur.execute("SELECT name FROM sqlite_master "
                          "WHERE type='table' AND name='strategy_backtest'").fetchone()
        if row is None:
            # 调用一次 save_backtest_stats 触发建表
            db.save_backtest_stats(
                strategy_id=rows[0]["id"], win_rate=70.0, profit_loss_ratio=2.0,
                total_signals=100, avg_ret=1.0, avg_holding_days=6.0,
                forward_returns={"1": {"pct_avg": 0.5, "win_rate": 55.0, "count": 100}},
                config={}, raw={},
            )
            cur = db._conn.cursor()
            row = cur.execute("SELECT name FROM sqlite_master "
                              "WHERE type='table' AND name='strategy_backtest'").fetchone()
        assert row is not None, "strategy_backtest 表未创建（即使调用 save_backtest_stats 后）"
        # 看有没有 B1海选
        b1 = [r for r in rows if r.get("name") == "B1海选"]
        if b1:
            print(f"  → 找到 B1海选 id={b1[0]['id']}, code={b1[0].get('code')}, "
                  f"backtest_win_rate={b1[0].get('backtest_win_rate')}")
        db.close()
        print("✅ test_real_db_load 通过")
    except Exception as e:
        raise


if __name__ == "__main__":
    print("=" * 60)
    print("数据库迁移冒烟自测 (v0.9.0)")
    print("=" * 60)
    try:
        test_db_migration_with_existing_db()
        test_real_db_load()
        print()
        print("🎉 数据库迁移测试通过")
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
