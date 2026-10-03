# 雇主养老缴费追溯结算库

工资更正或国别年度封顶额调整后，对受影响月份补记差额（只追加、不覆盖旧账）。
纯 Python 3 标准库实现，持久化仅用 SQLite，无第三方依赖。

## 计算规则

- 每年单独计算：某月累计应缴额 = min(该年截至当月累计工资, 国别年度封顶额) × 10%，按分四舍五入。
- 月应缴额 = 相邻两月累计应缴额之差，年初以前为零。
- 提交后，截止月以内每月的「初始账面额 + 全部历史补差」恰好等于当前输入算出的月应缴额。

## 接口

```python
from pension_settlement import PensionSettlement

s = PensionSettlement("settle.db")          # 打开或创建 SQLite 库
s.initialize(employees, wages, caps, (2024, 3))
#   employees: [(emp_id, country)]
#   wages:     [(emp_id, year, month, amount)]  每员工连续两个完整年度 24 个月
#   caps:      [(country, year, cap)]           每个国家两个年度
#   截止月以内视为已按初值结清

s.amend_wage("E1", 2024, 1, 45000)          # 替换某员工某月工资（一轮可多次，以最后值为准）
s.amend_cap("CN", 2024, 55000)              # 替换某国家某年度封顶额

batch = s.prepare((2024, 4))                # 截止月只能不变或前进一个月；返回非零补差，不入账
s.commit(batch)                             # 只追加补差明细并推进截止月；空批次也能提交
s.close()                                   # 修改、准备结果、已提交状态均已持久化，可随时关闭重开

s = PensionSettlement("settle.db")          # 重开
pending = s.pending_batch()                 # 取回重开前未提交的 prepare 结果（无则 None）
if pending:
    s.commit(pending)
```

状态机：`clean --prepare--> prepared --commit--> clean`。
`amend_*` 仅在 clean 状态可用；prepare 后只能 commit 或关闭重开；每轮修改后只准备一次。

## 运行测试

```sh
python3 -m unittest discover -s tests -v
```

`tests/reference.py` 是独立编写的全量参考计算（不与库共享代码），测试覆盖：
封顶月前移/后移/恢复、年度总额不变但缴费月份改变时的正负补差、
修改后与准备后分别关闭重开再提交、跨年扩展截止月、空批次提交。
