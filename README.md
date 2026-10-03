# 雇主养老缴费追溯结算库

工资更正或国别年度封顶额调整后，对受影响月份补记差额（只追加、不覆盖旧账）。
单一缴费类型（雇主养老，费率 10%），员工所属国家固定，金额均为非负整数分。
持久化仅使用 SQLite，无任何外部服务。

## 计算规则

- 每年单独计算：某月累计应缴额 = min(当年累计工资, 年度封顶额) × 10%，按分四舍五入。
- 月应缴额 = 相邻两月累计应缴额之差，年初以前为零。
- 提交后，截止月以内每月的 初始账面额 + 全部历史补差 = 当前输入算出的月应缴额。

## 接口

```python
from pension_settlement import Settlement

st = Settlement("settlement.db")          # SQLite 文件路径，可重复打开
st.initialize(employees, wages, caps, cutoff)
# employees: {员工号: 国家}
# wages:     {员工号: {(年, 月): 工资分}}，须覆盖连续两个完整年度
# caps:      {国家: {年: 封顶额分}}
# cutoff:    (年, 月) 或 None；截止月以内视为已按初值结清

st.amend_wage("E1", 2024, 1, 200000)      # 替换某员工某月工资，一轮可多次，以最后值为准
st.amend_cap("CN", 2024, 480000)          # 替换某国家某年度封顶额

batch = st.prepare((2024, 4))             # 截止月只能不变或前进一个月；返回非零补差，不入账
st.commit(batch)                          # 只追加补差明细并推进截止月；空批次也能提交

st.pending_batch()                        # 关闭重开后取回未提交的准备结果
st.current_cutoff()
st.close()                                # 关闭重开不丢修改、准备结果或已提交状态
```

状态机约束：存在未提交的准备结果时，不能修改也不能再次准备，只能 `commit` 或 `close`。

## 运行测试

```bash
python3 -m pytest tests/ -q
```

测试包含独立的全量参考计算（`tests/test_settlement.py` 中的 `Reference`），覆盖：
封顶月份前移/后移/恢复、年度总额不变时的正负补差、修改后与准备后关闭重开、
跨年扩展截止月、空批次提交、状态机与四舍五入。
