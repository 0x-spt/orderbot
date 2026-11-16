"""The 1C session is the operator's own Microsoft Remote Desktop window (mac_window.py)."""


class SessionLost(Exception):
    """The RDP window is gone, cannot be captured or driven, or was never found."""


class ShuttingDown(Exception):
    """The robot is being stopped by its operator: not a fault of 1C, nothing to report."""


class TransientOverlay(Exception):
    """Something passing covers or takes the focus from the RDP window right now (a
    notification banner, a window that did not come to front). Nothing was sent for the
    blocked unit; the pass ends quietly and the order is tried again on the next tick."""
