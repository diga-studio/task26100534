"""异常类型定义。"""


class SettlementError(Exception):
    """库内所有业务异常的基类。"""


class StateError(SettlementError):
    """操作与当前状态机不匹配（如存在未提交的准备结果时又修改）。"""


class CutoffError(SettlementError):
    """截止月不合法（倒退或一次前进超过一个月）。"""


class NotInitializedError(SettlementError):
    """尚未 initialize 就调用其他操作。"""
