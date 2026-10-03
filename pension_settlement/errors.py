"""结算库异常类型定义。"""


class SettlementError(Exception):
    """所有结算库异常的基类。"""


class StateError(SettlementError):
    """当前状态不允许执行该操作。"""


class CutoffError(SettlementError):
    """截止月不合法（倒退或跨越超过一个月）。"""
